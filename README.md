# cron-lint

A crontab checker that reads your schedule files and tells you which jobs will never run, which ones run more often than you think, and which lines cron silently throws away.

cron gives you no feedback. A malformed line is dropped, an impossible date never fires, an unescaped percent sign eats the rest of the command, and a file without a trailing newline loses its last job. cron-lint reads the file the way cron does and reports all of it before the job quietly fails.

## What it checks

- Field values outside the range cron accepts, such as `60` in the minute field, and steps such as `*/0` that make a job never fire.
- Reversed ranges like `22-4`, which cron wraps past the end of the field rather than rejecting.
- Impossible calendar combinations such as day 30 of February, or day 31 restricted to April, which produce a job that is scheduled but never runs.
- Day of month and day of week both restricted, where cron ORs the two fields and the job fires far more often than the author expected.
- Quartz syntax (`?`, `L`, `W`, `#`) that plain cron does not implement, with named values such as `WED` and `JUL` correctly left alone.
- The wrong crontab format: a five field user line dropped into `/etc/cron.d`, where the command is then read as a user name.
- Environment lines written as `MAILTO = root`, which cron ignores because of the spaces around the equals sign.
- Unescaped percent signs, commands with no output redirection, duplicate schedules, unknown `@macro` names, and a missing final newline.

## Install

```
pip install .
```

Or run it straight from the checkout without installing anything, since it only uses the standard library:

```
python3 cronlint.py /etc/crontab
```

## Usage

```
cron-lint /etc/crontab /etc/cron.d ~/my.crontab
cron-lint --strict /etc/cron.d
cron-lint --json /etc/crontab
```

Pass files or directories. A directory is scanned one level deep, which is what `/etc/cron.d` and a backup directory of crontabs both need.

Exit codes: `0` when nothing was found, `1` when there is an error, `1` for a warning as well when `--strict` is given, and `2` when no file could be read.

Text output puts one finding per line with the line number, the level, a stable code and a message:

```
/etc/cron.d/backup:
  line 3    error    bad-field          minute field: 60-60 sits outside the valid range 0-59
  line 4    error    impossible-date    day of month 30 never exists in February, so the job never runs
  line 5    info     no-redirect        no output redirection, cron mails anything the job prints

2 error(s), 0 warning(s), 1 note(s)
```

The `code` values are stable, so `--json` output can be piped into a CI check or a monitoring script.

## Tests

```
python3 -m unittest discover -s tests -v
```

The suite covers field parsing, every check, the system crontab format, and the CLI exit codes.

## Notes on scope

cron-lint is a static checker. It does not run jobs, does not read the cron daemon state, and does not try to resolve whether the command on the line exists.

## License

MIT, see LICENSE.
