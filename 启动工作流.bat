@echo off
rem ===================================================================
rem Daily unattended workflow: translate -> self-check -> self-fix
rem ===================================================================
rem
rem Runs 9:00-20:00 and exits by itself after 20:00. Run it once in the
rem morning and forget about it. Close this window to stop early.
rem
rem What it does each round (about 70 minutes per round):
rem   1. translate a batch (translate_batch)
rem   2. full self-check (audit_translation)
rem   3. rule-based fixes (fix_org_names / fix_terms)
rem   4. targeted retranslation (retranslate)
rem
rem NOTE: keep this file ASCII-only. Chinese Windows cmd parses .bat as
rem GBK, so non-ASCII comments turn into garbage and can even be run as
rem commands. There is a test for this:
rem tests/test_scripts.py::test_bat_files_are_ascii_only
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\auto_workflow.py
pause
