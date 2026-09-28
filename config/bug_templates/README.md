# BUG process mail templates

Edit these to match your organisation's formats. Format: first line `Subject: ...`, a blank line, then the body.
Only the placeholders below are filled in; anything else (including other braces) is kept as you wrote it.
A placeholder with no value stays visible (e.g. `{scan_date}`), and the CLI warns you before saving the draft.

| File | Sent at step | To |
|---|---|---|
| security.md | security_mail | recipients.security |
| tester.md | tester_mail | recipients.tester |
| pm_signoff.md | signoff_mails | recipients.pm |
| keyleader_signoff.md | signoff_mails | recipients.keyleader |

## Placeholders
- `{bug_id}` `{bug_title}` `{my_name}` `{recipient_name}` `{today}`
- Scan: `{application}` `{scan_name}` `{scan_date}` `{severity_table}` `{severity_line}`
  `{critical}` `{high}` `{medium}` `{low}` `{informational}` `{total}`
  `{security_gate}` (PASS/FAIL) `{security_gate_reason}` `{findings_list}` `{scan_summary}`
- Rounds: `{round}` (1, 2, ...), `{fix_note}` (what you fixed, from `agent bug restart --fix`),
  `{resubmission_note}` (a ready-made sentence in round 2+, empty in round 1)
- Progress: `{security_approval_by}` `{security_approval_date}` `{security_mail_date}` `{tester_mail_date}` `{tester_approval_by}` `{tester_approval_date}`
  `{tester_signoff_by}` `{tester_signoff_date}` `{tester_conditions}`

`README.md` in this folder is not a template.
