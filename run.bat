@echo off
setlocal

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "MARKER=%VENV%\.installed"

:: 가상환경 없으면 생성
if not exist "%PYTHON%" (
    echo [setup] 가상환경 생성 중...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [오류] 가상환경 생성 실패. Python이 설치되어 있는지 확인하세요.
        pause
        exit /b 1
    )
    echo [setup] 가상환경 생성 완료.
    if exist "%MARKER%" del "%MARKER%"
)

:: requirements 미설치 시 설치
if not exist "%MARKER%" (
    echo [setup] 패키지 설치 중...
    "%PIP%" install -r "%ROOT%requirements.txt" --quiet
    if errorlevel 1 (
        echo [오류] 패키지 설치 실패.
        pause
        exit /b 1
    )
    echo installed > "%MARKER%"
    echo [setup] 패키지 설치 완료.
)

:: 인자 없으면 chat, 있으면 그대로 전달
if "%~1"=="" (
    "%PYTHON%" "%ROOT%run.py" chat
) else (
    "%PYTHON%" "%ROOT%run.py" %*
)
