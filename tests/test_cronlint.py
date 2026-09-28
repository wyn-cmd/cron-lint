#!/usr/bin/env python3
# Unit tests for cron-lint. Run with: python3 -m unittest discover -s tests
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cronlint


def codes(findings, level=None):
    return sorted(item.code for item in findings if level is None or item.level == level)


class FieldParsingTests(unittest.TestCase):
    def test_star_matches_everything(self):
        values, restricted, errors = cronlint.parse_field("*", "minute", 0, 59, {})
        self.assertEqual(values, set(range(0, 60)))
        self.assertFalse(restricted)
        self.assertEqual(errors, [])

    def test_step_over_star(self):
        values, _, errors = cronlint.parse_field("*/15", "minute", 0, 59, {})
        self.assertEqual(values, {0, 15, 30, 45})
        self.assertEqual(errors, [])

    def test_names_and_ranges(self):
        values, _, errors = cronlint.parse_field("mon-fri", "day of week", 0, 7, cronlint.DOW_NAMES)
        self.assertEqual(values, {1, 2, 3, 4, 5})
        self.assertEqual(errors, [])

    def test_sunday_normalises_seven_to_zero(self):
        values, _, _ = cronlint.parse_field("7", "day of week", 0, 7, cronlint.DOW_NAMES)
        self.assertEqual(values, {0})
        values, _, _ = cronlint.parse_field("0-7", "day of week", 0, 7, cronlint.DOW_NAMES)
        self.assertEqual(values, set(range(0, 7)))

    def test_zero_step_is_an_error(self):
        _, _, errors = cronlint.parse_field("*/0", "minute", 0, 59, {})
        self.assertTrue(any("step of 0" in item for item in errors))

    def test_out_of_range_value(self):
        _, _, errors = cronlint.parse_field("60", "minute", 0, 59, {})
        self.assertTrue(any("outside the valid range" in item for item in errors))

    def test_unknown_name(self):
        _, _, errors = cronlint.parse_field("funday", "day of week", 0, 7, cronlint.DOW_NAMES)
        self.assertTrue(any("not a number or a known name" in item for item in errors))


class LintTests(unittest.TestCase):
    def lint(self, text, path="/home/wyn/crontab"):
        return cronlint.lint_text(text, path)

    def test_clean_crontab_reports_nothing(self):
        text = "# nightly backup\n17 3 * * * /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1\n"
        self.assertEqual(self.lint(text), [])

    def test_minute_out_of_range(self):
        findings = self.lint("60 3 * * * /bin/true >/dev/null 2>&1\n")
        self.assertIn("bad-field", codes(findings, "error"))

    def test_reversed_range_is_flagged(self):
        findings = self.lint("0 22-4 * * * /bin/true >/dev/null 2>&1\n")
        self.assertTrue(any("reversed" in item.message for item in findings))

    def test_february_thirtieth_never_runs(self):
        findings = self.lint("0 0 30 2 * /bin/true >/dev/null 2>&1\n")
        self.assertIn("impossible-date", codes(findings, "error"))

    def test_full_month_with_day_31_does_not_warn(self):
        findings = self.lint("0 0 31 * * /bin/true >/dev/null 2>&1\n")
        self.assertEqual(findings, [])

    def test_day_31_in_april_is_an_error(self):
        findings = self.lint("0 0 31 4 * /bin/true >/dev/null 2>&1\n")
        self.assertIn("impossible-date", codes(findings, "error"))

    def test_dom_and_dow_both_restricted(self):
        findings = self.lint("0 0 1 * 1 /bin/true >/dev/null 2>&1\n")
        self.assertIn("dom-and-dow", codes(findings, "info"))

    def test_quartz_characters_rejected(self):
        findings = self.lint("0 0 ? * * /bin/true >/dev/null 2>&1\n")
        self.assertIn("quartz-syntax", codes(findings, "error"))

    def test_weekday_and_month_names_are_not_quartz(self):
        findings = self.lint("0 9 * jul wed /bin/true >/dev/null 2>&1\n")
        self.assertNotIn("quartz-syntax", codes(findings))

    def test_unescaped_percent(self):
        findings = self.lint("0 5 * * * /bin/date +%Y >/dev/null\n")
        self.assertIn("unescaped-percent", codes(findings, "warning"))

    def test_missing_final_newline(self):
        findings = self.lint("0 5 * * * /bin/true >/dev/null 2>&1")
        self.assertIn("no-final-newline", codes(findings, "warning"))

    def test_env_line_with_spaces_is_ignored_by_cron(self):
        findings = self.lint("MAILTO = root\n0 5 * * * /bin/true >/dev/null 2>&1\n")
        self.assertIn("env-spacing", codes(findings, "warning"))

    def test_plain_env_line_is_accepted(self):
        findings = self.lint("MAILTO=root\nSHELL=/bin/bash\n0 5 * * * /bin/true >/dev/null 2>&1\n")
        self.assertEqual(findings, [])

    def test_unknown_macro(self):
        findings = self.lint("@fortnightly /bin/true >/dev/null 2>&1\n")
        self.assertIn("unknown-macro", codes(findings, "error"))

    def test_known_macro_is_fine(self):
        findings = self.lint("@daily /usr/local/bin/report.sh >/dev/null 2>&1\n")
        self.assertEqual(findings, [])

    def test_duplicate_schedule(self):
        text = "0 4 * * * /usr/local/bin/a.sh >/dev/null 2>&1\n0 4 * * * /usr/local/bin/b.sh >/dev/null 2>&1\n"
        findings = self.lint(text)
        self.assertIn("duplicate-schedule", codes(findings, "info"))

    def test_missing_user_field_in_system_crontab(self):
        findings = self.lint("0 4 * * * /usr/local/bin/a.sh\n", "/etc/cron.d/backup")
        self.assertIn("missing-user-field", codes(findings, "error"))

    def test_redirect_read_as_user_column(self):
        findings = self.lint("0 4 * * * /usr/local/bin/a.sh >/dev/null 2>&1\n", "/etc/cron.d/backup")
        self.assertIn("bad-user-field", codes(findings, "error"))

    def test_system_crontab_with_user_is_clean(self):
        findings = self.lint("0 4 * * * root /usr/local/bin/a.sh >/dev/null 2>&1\n", "/etc/cron.d/backup")
        self.assertEqual(findings, [])

    def test_five_field_line_without_command(self):
        findings = self.lint("0 4 * * *\n")
        self.assertIn("field-count", codes(findings, "error"))

    def test_no_redirect_note(self):
        findings = self.lint("0 4 * * * /usr/local/bin/a.sh\n")
        self.assertIn("no-redirect", codes(findings, "info"))


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = cronlint.main(argv)
        return status, buffer.getvalue()

    def test_clean_file_exits_zero(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "clean.crontab")
        status, output = self.run_cli([path])
        self.assertEqual(status, 0)
        self.assertIn("clean", output)

    def test_broken_file_exits_one_and_prints_json(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "broken.crontab")
        status, output = self.run_cli(["--json", path])
        self.assertEqual(status, 1)
        payload = json.loads(output)
        self.assertEqual(len(payload["results"]), 1)
        self.assertTrue(payload["results"][0]["findings"])

    def test_strict_mode_fails_on_warnings(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "warning.crontab")
        status, _ = self.run_cli([path])
        self.assertEqual(status, 0)
        status, _ = self.run_cli(["--strict", path])
        self.assertEqual(status, 1)

    def test_directory_of_crontabs(self):
        directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        status, output = self.run_cli(["--quiet", directory])
        self.assertIn("error(s)", output)
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
