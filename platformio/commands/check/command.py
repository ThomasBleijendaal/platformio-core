# Copyright (c) 2019-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=too-many-arguments,too-many-locals,too-many-branches
# pylint: disable=redefined-builtin,too-many-statements

import os
from collections import Counter
from os.path import basename, dirname, isfile, join
from time import time

import click
from tabulate import tabulate

from platformio import exception, fs, util
from platformio.commands.check.tools import CheckToolFactory
from platformio.commands.check.defect import DefectItem
from platformio.compat import dump_json_to_unicode
from platformio.project.config import ProjectConfig
from platformio.project.helpers import (find_project_dir_above,
                                        get_project_dir,
                                        get_project_include_dir,
                                        get_project_src_dir)


@click.command("check", short_help="Run a static analysis tool on code")
@click.option("-e", "--environment", multiple=True)
@click.option("-d",
              "--project-dir",
              default=os.getcwd,
              type=click.Path(exists=True,
                              file_okay=True,
                              dir_okay=True,
                              writable=True,
                              resolve_path=True))
@click.option("-c",
              "--project-conf",
              type=click.Path(exists=True,
                              file_okay=True,
                              dir_okay=False,
                              readable=True,
                              resolve_path=True))
@click.option("--filter", multiple=True, help="Pattern: +<include> -<exclude>")
@click.option("--flags", multiple=True)
@click.option("--severity",
              multiple=True,
              type=click.Choice(DefectItem.SEVERITY_LABELS.values()))
@click.option("-s", "--silent", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--json-output", is_flag=True)
def cli(environment, project_dir, project_conf, filter, flags, severity,
        silent, verbose, json_output):
    # find project directory on upper level
    if isfile(project_dir):
        project_dir = find_project_dir_above(project_dir)

    results = []
    with fs.cd(project_dir):
        config = ProjectConfig.get_instance(
            project_conf or join(project_dir, "platformio.ini"))
        config.validate(environment)

        default_envs = config.default_envs()
        for envname in config.envs():
            skipenv = any([
                environment and envname not in environment, not environment
                and default_envs and envname not in default_envs
            ])

            env_options = config.items(env=envname, as_dict=True)
            env_dump = []
            for k, v in env_options.items():
                if k not in ("platform", "framework", "board"):
                    continue
                env_dump.append(
                    "%s: %s" % (k, ", ".join(v) if isinstance(v, list) else v))

            default_filter = [
                "+<%s/>" % basename(d)
                for d in (get_project_src_dir(), get_project_include_dir())
            ]

            tool_options = dict(
                verbose=verbose,
                silent=silent,
                filter=filter
                or env_options.get("check_filter", default_filter),
                flags=flags or env_options.get("check_flags"),
                severity=[
                    DefectItem.SEVERITY_LABELS[DefectItem.SEVERITY_HIGH]
                ] if silent else
                (severity or env_options.get("check_severity")))

            for tool in env_options.get("check_tool", ["cppcheck"]):
                if skipenv:
                    results.append({"env": envname, "tool": tool})
                    continue
                if not silent and not json_output:
                    print_processing_header(tool, envname, env_dump)

                ct = CheckToolFactory.new(tool, project_dir, config, envname,
                                          tool_options)

                result = {"env": envname, "tool": tool, "duration": time()}
                rc = ct.check(on_defect_callback=None if (
                    json_output or verbose
                ) else lambda defect: click.echo(repr(defect)))

                result['defects'] = ct.get_defects()
                result['duration'] = time() - result['duration']
                result['succeeded'] = (
                    rc == 0 and not any(d.severity == DefectItem.SEVERITY_HIGH
                                        for d in result['defects']))
                results.append(result)

                if verbose:
                    click.echo("\n".join(repr(d) for d in result['defects']))

                if not json_output and not silent:
                    if not result['defects']:
                        click.echo("No defects found")
                    print_processing_footer(result)

    if json_output:
        click.echo(dump_json_to_unicode(results_to_json(results)))
    elif not silent:
        print_check_summary(results)

    command_failed = any(r.get("succeeded") is False for r in results)
    if command_failed:
        raise exception.ReturnErrorCode(1)


def results_to_json(raw):
    results = []
    for item in raw:
        item.update({
            "ignored": item.get("succeeded") is None,
            "succeeded": bool(item.get("succeeded")),
            "defects": [d.to_json() for d in item.get("defects", [])]
        })
        results.append(item)

    return results


def print_processing_header(tool, envname, envdump):
    click.echo(
        "Checking %s > %s (%s)" %
        (click.style(envname, fg="cyan", bold=True), tool, "; ".join(envdump)))
    terminal_width, _ = click.get_terminal_size()
    click.secho("-" * terminal_width, bold=True)


def print_processing_footer(result):
    is_failed = not result.get("succeeded")
    util.print_labeled_bar(
        "[%s] Took %.2f seconds" %
        ((click.style("FAILED", fg="red", bold=True) if is_failed else
          click.style("PASSED", fg="green", bold=True)), result['duration']),
        is_error=is_failed)


def print_defects_stats(results):
    components = dict()

    def _append_defect(component, defect):
        if not components.get(component):
            components[component] = Counter()
        components[component].update(
            {DefectItem.SEVERITY_LABELS[defect.severity]: 1})

    for result in results:
        for defect in result.get("defects", []):
            component = dirname(defect.file) or defect.file
            _append_defect(component, defect)

            if component.startswith(get_project_dir()):
                while os.sep in component:
                    component = dirname(component)
                    _append_defect(component, defect)

    if not components:
        return

    severity_labels = list(DefectItem.SEVERITY_LABELS.values())
    severity_labels.reverse()
    tabular_data = list()
    for k, v in components.items():
        tool_defect = [v.get(s, 0) for s in severity_labels]
        tabular_data.append([k] + tool_defect)

    total = ["Total"] + [sum(d) for d in list(zip(*tabular_data))[1:]]
    tabular_data.sort()
    tabular_data.append([])  # Empty line as delimeter
    tabular_data.append(total)

    headers = ["Component"]
    headers.extend([l.upper() for l in severity_labels])
    headers = [click.style(h, bold=True) for h in headers]
    click.echo(tabulate(tabular_data, headers=headers, numalign="center"))
    click.echo()


def print_check_summary(results):
    click.echo()

    tabular_data = []
    succeeded_nums = 0
    failed_nums = 0
    duration = 0

    print_defects_stats(results)

    for result in results:
        duration += result.get("duration", 0)
        if result.get("succeeded") is False:
            failed_nums += 1
            status_str = click.style("FAILED", fg="red")
        elif result.get("succeeded") is None:
            status_str = "IGNORED"
        else:
            succeeded_nums += 1
            status_str = click.style("PASSED", fg="green")

        tabular_data.append(
            (click.style(result['env'], fg="cyan"), result['tool'], status_str,
             util.humanize_duration_time(result.get("duration"))))

    click.echo(tabulate(tabular_data,
                        headers=[
                            click.style(s, bold=True)
                            for s in ("Environment", "Tool", "Status",
                                      "Duration")
                        ]),
               err=failed_nums)

    util.print_labeled_bar(
        "%s%d succeeded in %s" %
        ("%d failed, " % failed_nums if failed_nums else "", succeeded_nums,
         util.humanize_duration_time(duration)),
        is_error=failed_nums,
        fg="red" if failed_nums else "green")
