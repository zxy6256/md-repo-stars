@echo off
rem md-repo-stars launcher: rank GitHub repos found in a markdown file by stars.
rem Usage: md-repo-stars.cmd README.md [options]
python "%~dp0md_repo_stars.py" %*
