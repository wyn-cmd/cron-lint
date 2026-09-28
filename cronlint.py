#!/usr/bin/env python3
# cron-lint is a static analyzer for crontab files. It parses every schedule,
# checks the values against the limits cron actually enforces, and reports the
# traps that make a job silently never run, run twice, or fail because the line
# was written in the wrong crontab format.
import argparse
import json
import os
import re
import sys

MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

FIELD_SPECS = [
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, MONTH_NAMES),
    ("day of week", 0, 7, DOW_NAMES),
]

MACROS = {
    "@reboot", "@yearly", "@annually", "@monthly",
    "@weekly", "@daily", "@midnight", "@hourly",
}

# month number to its longest possible day count, February keeps 29 for leap years
MONTH_LENGTHS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
LONG_MONTHS = {1, 3, 5, 7, 8, 10, 12}
THIRTY_DAY_MONTHS = {4, 6, 9, 11}

# quartz style extras that plain cron does not implement
QUARTZ_CHARS = {
    "?": "question mark is Quartz syntax, cron needs a value or an asterisk",
    "L": "L (last day) is Quartz syntax and cron does not support it",
    "W": "W (nearest weekday) is Quartz syntax and cron does not support it",
    "#": "# (nth weekday) is Quartz syntax and cron does not support it",
}

USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$", re.IGNORECASE)
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INT_RE = re.compile(r"^\d+$")


class Finding:
    def __init__(self, level, code, line, message):
        self.level = level
        self.code = code
        self.line = line
        self.message = message

    def as_dict(self):
        return {"level": self.level, "code": self.code, "line": self.line, "message": self.message}


def expand_token(token, name, low, high, names):
    # returns (values, restricted, errors) for a single comma separated element
    errors = []
    step = None
    body = token
    if "/" in token:
        body, _, step_text = token.partition("/")
        if not INT_RE.match(step_text):
            return set(), True, ["%s field: step %r is not a number" % (name, step_text)]
        step = int(step_text)
        if step == 0:
            return set(), True, ["%s field: a step of 0 makes the job never run" % name]

    def value_of(text):
        lowered = text.lower()
        if lowered in names:
            return names[lowered]
        if INT_RE.match(text):
            return int(text)
        return None

    if body == "*":
        start, end = low, high
        restricted = step is not None and step != 1
    elif "-" in body.lstrip("-"):
        left, _, right = body.partition("-")
        start = value_of(left)
        end = value_of(right)
        if start is None or end is None:
            return set(), True, ["%s field: %r is not a number or a known name" % (name, token)]
        restricted = True
        if start > end:
            errors.append("%s field: range %s-%s is reversed, cron wraps it past the end of the field" % (name, left, right))
    else:
        single = value_of(body)
        if single is None:
            return set(), True, ["%s field: %r is not a number or a known name" % (name, token)]
        start = single
        end = high if step is not None else single
        restricted = True

    if start < low or end > high:
        return set(), True, ["%s field: %d-%d sits outside the valid range %d-%d" % (name, start, end, low, high)]

    stride = step if step else 1
    values = set(range(start, end + 1, stride))
    # day of week accepts both 0 and 7 for Sunday, normalise onto 0
    if high == 7 and 7 in values:
        values.discard(7)
        values.add(0)
    return values, restricted, errors


def parse_field(expr, name, low, high, names):
    # returns (values, restricted, errors)
    values = set()
    errors = []
    restricted = False
    if expr == "":
        return values, True, ["%s field is empty" % name]
    for token in expr.split(","):
        if token == "":
            errors.append("%s field: empty element in the list %r" % (name, expr))
            continue
        element_values, element_restricted, element_errors = expand_token(token, name, low, high, names)
        errors.extend(element_errors)
        values |= element_values
        if element_restricted:
            restricted = True
    if not errors and not values:
        errors.append("%s field matches nothing, so the job never runs" % name)
    return values, restricted, errors


def find_quartz(expr, names):
    # named values such as WED and JUL contain the letters W and L, so strip the
    # names out before looking for Quartz only characters
    scrubbed = expr.lower()
    for word in sorted(names, key=len, reverse=True):
        scrubbed = scrubbed.replace(word, "")
    found = set()
    for character in QUARTZ_CHARS:
        if character in scrubbed:
            found.add(character)
    return found


def is_system_crontab(path):
    # only /etc/crontab and /etc/cron.d/* use the six field format with a user
    # name column, a crontab that happens to be called crontab is still a user file
    absolute = os.path.abspath(path)
    base = os.path.basename(absolute)
    parent = os.path.basename(os.path.dirname(absolute))
    return absolute == "/etc/crontab" or parent == "cron.d" or base.startswith("cron.d.")


def check_calendar(schedule, line_no, findings):
    dom_values, dom_restricted = schedule["day of month"]
    month_values, month_restricted = schedule["month"]
    if not dom_restricted or not dom_values:
        return
    hot = set(month_values) if month_restricted else set(range(1, 13))
    if not hot:
        return
    biggest_dom = max(dom_values)
    if hot == {2} and biggest_dom >= 30:
        findings.append(Finding("error", "impossible-date", line_no,
                                "day of month %d never exists in February, so the job never runs" % biggest_dom))
        return
    if biggest_dom == 31 and hot.issubset(THIRTY_DAY_MONTHS | {2}):
        findings.append(Finding("error", "impossible-date", line_no,
                                "day of month 31 never exists in the selected months, so the job never runs"))
        return
    if biggest_dom == 31 and month_restricted:
        overlap = sorted(value for value in hot if value not in LONG_MONTHS)
        if overlap:
            findings.append(Finding("warning", "impossible-date", line_no,
                                    "day of month 31 does not exist in month(s) %s, the job is skipped there"
                                    % ",".join(str(value) for value in overlap)))


def check_command(command, line_no, findings):
    if not command.strip():
        findings.append(Finding("error", "empty-command", line_no, "this schedule has no command to run"))
        return
    if "%" in command and "\\%" not in command:
        findings.append(Finding("warning", "unescaped-percent", line_no,
                                "an unescaped percent sign becomes a newline in cron, the rest of the line is fed to stdin, escape it as \\%"))
    if ">" not in command:
        findings.append(Finding("info", "no-redirect", line_no,
                                "no output redirection, cron mails anything the job prints"))


def analyze_line(line, line_no, system_format, findings):
    # returns (schedule key, command) when the line holds a real schedule
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("@"):
        macro = stripped.split()[0].lower()
        if macro not in MACROS:
            findings.append(Finding("error", "unknown-macro", line_no,
                                    "%s is not a cron macro, the valid ones are %s" % (macro, ", ".join(sorted(MACROS)))))
        return None

    if "=" in stripped:
        first_token = stripped.split()[0]
        if "=" in first_token and ENV_RE.match(first_token.split("=", 1)[0]):
            # a proper VAR=value line, cron reads it as an environment setting
            return None
        left = stripped.split("=", 1)[0].strip()
        if ENV_RE.match(first_token) and left == first_token:
            findings.append(Finding("warning", "env-spacing", line_no,
                                    "cron only honours VAR=value with no spaces around the equals sign, this line sets nothing"))
            return None

    parts = stripped.split()
    needed = 6 if system_format else 5
    if len(parts) < needed + 1:
        if system_format and len(parts) == 6:
            findings.append(Finding("error", "missing-user-field", line_no,
                                    "this directory uses the system crontab format, a user name belongs between the schedule and the command"))
            return None
        findings.append(Finding("error", "field-count", line_no,
                                "expected %d schedule fields plus a command, found %d fields" % (needed, len(parts))))
        return None

    schedule_parts = parts[:5]
    index = 5
    if system_format:
        user = parts[5]
        index = 6
        if "/" in user or not USER_RE.match(user):
            findings.append(Finding("error", "bad-user-field", line_no,
                                    "%r is not a user name, this directory uses the six field format so a user name belongs between the schedule and the command" % user))

    command = " ".join(parts[index:])
    schedule = {}
    for (name, low, high, names), expr in zip(FIELD_SPECS, schedule_parts):
        quartz = sorted(find_quartz(expr, names))
        for character in quartz:
            findings.append(Finding("error", "quartz-syntax", line_no,
                                    "%s field: %s" % (name, QUARTZ_CHARS[character])))
        if quartz:
            # the quartz message already explains the field, do not also call the
            # same token an unknown value
            schedule[name] = (set(), True)
            continue
        values, restricted, errors = parse_field(expr, name, low, high, names)
        for error in errors:
            findings.append(Finding("error", "bad-field", line_no, error))
        schedule[name] = (values, restricted)

    dom_values, dom_restricted = schedule["day of month"]
    dow_values, dow_restricted = schedule["day of week"]
    if dom_restricted and dow_restricted:
        findings.append(Finding("info", "dom-and-dow", line_no,
                                "day of month and day of week are both restricted, cron ORs them, so the job runs on either match"))
    check_calendar(schedule, line_no, findings)
    check_command(command, line_no, findings)
    return " ".join(schedule_parts), command


def lint_text(text, path="<stdin>"):
    findings = []
    system_format = is_system_crontab(path)
    schedules = {}
    lines = text.splitlines()
    for offset, line in enumerate(lines, start=1):
        key = analyze_line(line, offset, system_format, findings)
        if key is None:
            continue
        schedule_key = key[0]
        if schedule_key in schedules:
            findings.append(Finding("info", "duplicate-schedule", offset,
                                    "same schedule as line %d, both jobs fire at the same instant" % schedules[schedule_key]))
        else:
            schedules[schedule_key] = offset
    if text and not text.endswith("\n"):
        findings.append(Finding("warning", "no-final-newline", len(lines),
                                "the last line has no trailing newline, cron ignores a line the file does not terminate"))
    return sorted(findings, key=lambda item: (item.line, item.code))


def lint_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return lint_text(text, path)


def collect_targets(paths):
    targets = []
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if os.path.isfile(full):
                    targets.append(full)
        else:
            targets.append(path)
    return targets


def count_levels(results):
    totals = {"error": 0, "warning": 0, "info": 0}
    for _, findings in results:
        for finding in findings:
            totals[finding.level] += 1
    return totals


def render_text(results):
    lines_out = []
    for path, findings in results:
        if not findings:
            lines_out.append("%s: clean" % path)
            continue
        lines_out.append("%s:" % path)
        for finding in findings:
            lines_out.append("  line %-4d %-8s %-18s %s" % (finding.line, finding.level, finding.code, finding.message))
    totals = count_levels(results)
    lines_out.append("")
    lines_out.append("%d error(s), %d warning(s), %d note(s)" % (totals["error"], totals["warning"], totals["info"]))
    return "\n".join(lines_out), totals


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cron-lint",
        description="Check crontab files for schedules that never run, run the wrong amount, or use the wrong field format.",
    )
    parser.add_argument("files", nargs="+", help="crontab files, or directories such as /etc/cron.d")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    parser.add_argument("--strict", action="store_true", help="exit non-zero on warnings as well as errors")
    parser.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = parser.parse_args(argv)

    results = []
    for target in collect_targets(args.files):
        try:
            results.append((target, lint_file(target)))
        except OSError as error:
            results.append((target, [Finding("error", "unreadable", 0, str(error))]))
    if not results:
        print("no crontab files found", file=sys.stderr)
        return 2

    if args.json:
        payload = {"results": [{"path": path, "findings": [item.as_dict() for item in findings]} for path, findings in results]}
        print(json.dumps(payload, indent=2))
        totals = count_levels(results)
    else:
        report, totals = render_text(results)
        print(report.strip().splitlines()[-1] if args.quiet else report)

    if totals["error"]:
        return 1
    if args.strict and totals["warning"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
