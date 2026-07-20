@echo off
REM run_all.bat -- launches the daemon and the dashboard, each in its own
REM window, each in the truthmon conda env. They stay two processes (see
REM CLAUDE.md/spec for why -- WAL SQLite, daemon-only writes, dashboard
REM read-only) but you only run one command to start both.

cd /d "%~dp0"

start "Truth Monitor - Daemon" cmd /k "call conda activate truthmon && python truth_monitor.py"

REM conda activate writes a temp file per invocation -- two activations
REM launched back-to-back can collide over it ("process cannot access the
REM file... __conda_tmp_*.txt"). Stagger the second one.
timeout /t 10 /nobreak >nul

start "Truth Monitor - Dashboard" cmd /k "call conda activate truthmon && streamlit run app.py"
