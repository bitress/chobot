@echo off
setlocal
cd /d "%~dp0\.."

echo Installing ChoBot self-hosted toolkit dependencies...
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist .env (
  copy .env.example .env
  echo Created .env from .env.example. Edit .env before starting ChoBot.
)

echo.
echo Install complete.
echo Next: edit .env, then run scripts\start_all.bat
pause
