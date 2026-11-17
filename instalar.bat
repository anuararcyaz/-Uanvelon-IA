@echo off
echo === Creando entorno virtual e instalando dependencias ===
python -m venv venv
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Listo. Ahora instala Ollama (https://ollama.com) y ejecuta:  ollama pull llama3.1
pause
