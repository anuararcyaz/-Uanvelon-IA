# Uanvelon – Asistente con herramientas

Un asistente en Python que **decide solo** qué herramienta usar y con qué parámetros:

| Herramienta | Qué hace |
|---|---|
| `obtener_clima` | Clima actual de una ciudad (Open-Meteo, sin API key) |
| `calcular` | Calculadora exacta y segura (AST, sin `eval`) |
| `buscar_web` | Búsqueda en internet (DuckDuckGo, sin API key) |
| `consultar_base_datos` | SELECT de solo lectura sobre una base SQLite de tienda |
| `fecha_hora_actual` | Fecha y hora del sistema |

## Instalación en Visual Studio Code

1. Abre la carpeta del proyecto Uanvelon en VS Code (`Archivo > Abrir carpeta`).
2. Abre una terminal (`Ctrl + ñ`) y crea el entorno virtual:
   ```bash
   python -m venv venv
   venv\Scripts\activate          # Windows
   # source venv/bin/activate     # Mac / Linux
   pip install -r requirements.txt
   ```
3. Selecciona el intérprete: `Ctrl+Shift+P` → *Python: Select Interpreter* → el de `venv`.

## Opción A – 100 % local con Ollama (gratis, por defecto)

1. Instala Ollama desde https://ollama.com
2. Descarga un modelo con soporte de herramientas:
   ```bash
   ollama pull llama3.1
   ```
3. Ejecuta:
   ```bash
   python agente.py
   ```
   (Otros modelos válidos: `qwen2.5:7b`, `mistral-nemo`. Úsalos con `--modelo`.)

## Opción B – Claude (API de Anthropic)

```bash
set ANTHROPIC_API_KEY=tu_clave        # Windows (CMD)
# export ANTHROPIC_API_KEY=tu_clave   # Mac / Linux
python agente.py --proveedor anthropic
```

## Preguntas de prueba

- `¿Qué clima hace hoy en Monterrey?` → usa **obtener_clima**
- `¿Cuánto es 18% de propina sobre 1,245.50?` → usa **calcular**
- `¿Qué productos tienen menos de 30 unidades en stock?` → usa **consultar_base_datos**
- `Busca las últimas noticias sobre inteligencia artificial` → usa **buscar_web**
- `¿Cuánto dinero generó cada producto en ventas?` → **consultar_base_datos** + **calcular**
- `Explícame qué es una API` → **no usa ninguna herramienta**

La consola muestra el ciclo **razonamiento → acción → observación** en cada paso.

## Interfaz web local

La página de chat se ejecuta en el equipo y, por defecto, utiliza Ollama:

```bash
python web.py
```

Abra `http://127.0.0.1:8000` en el navegador. En Windows también puede iniciar la interfaz con `ejecutar_web.bat`.
Puede seleccionar un modelo o un puerto diferentes:

```bash
python web.py --modelo qwen2.5:7b --puerto 8001
```

Para utilizar Anthropic, configure `ANTHROPIC_API_KEY` y ejecute `python web.py --proveedor anthropic`.
La página identifica al proveedor; con Ollama el modelo se ejecuta localmente, mientras que Anthropic procesa
las consultas en su servicio remoto. Las herramientas de clima y búsqueda web se conectan a servicios externos,
incluso al usar Ollama. La sesión de conversación se mantiene en memoria hasta detener el servidor.
En navegadores compatibles, el botón de micrófono transcribe la consulta al español; puede revisar el texto antes
de enviarlo. La transcripción depende del navegador y su audio puede ser procesado por el servicio de reconocimiento
de voz del navegador.

## Publicar en Render con acceso de Google

El archivo `render.yaml` prepara un servicio web gratuito en Render y selecciona Anthropic como proveedor, porque
un servicio publicado no puede acceder al Ollama instalado en su computadora. Las conversaciones se conservan
por usuario mientras el proceso está activo; el plan gratuito puede suspenderse y reiniciarse, por lo que las
sesiones en memoria no son permanentes. Render también puede iniciar la aplicación sin secretos para mostrar
la página de configuración, pero no permitirá iniciar sesión ni consultar la IA hasta que se configuren.

1. Suba esta carpeta a un repositorio público de GitHub y cree el servicio en Render desde ese repositorio,
   usando el Blueprint detectado en `render.yaml`. El uso de la API de Anthropic puede generar cargos.
2. En Google Cloud Console, configure la pantalla de consentimiento como **External / Testing** y agregue
   cada cuenta autorizada en la lista de **Test users**.
3. Cree un **OAuth client ID** de tipo **Web application**. En sus URI de redirección autorizadas agregue
   exactamente `https://<nombre-del-servicio>.onrender.com/auth/google/callback`.
4. En la sección **Environment** del servicio de Render, establezca estos valores privados:
   - `GOOGLE_CLIENT_ID`: ID del cliente OAuth de Google.
   - `GOOGLE_CLIENT_SECRET`: secreto del cliente OAuth de Google.
   - `GOOGLE_REDIRECT_URI`: la URL de retorno registrada en Google.
   - `APP_SECRET_KEY`: una clave aleatoria de al menos 32 caracteres. Genérela en su equipo con
     `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   - `ANTHROPIC_API_KEY`: su clave de API de Anthropic.
5. Después del despliegue, solo podrán ingresar las cuentas que Google permita en modo de prueba.

No escriba claves en el código ni las suba a GitHub. `render.yaml` marca estos valores como secretos para
introducirlos en el panel privado de Render; `.env` está excluido de Git. Para habilitar usuarios fuera de
la lista de prueba, habría que cambiar la configuración de publicación y acceso de Google.

## Alcance de las herramientas

El asistente utiliza la calculadora para operaciones matemáticas, consulta el clima mediante Open-Meteo,
busca información reciente en Internet y consulta productos y ventas en la base SQLite local. Solo puede
acceder a sistemas externos para los que exista una herramienta integrada y autorizada; otros servicios
requieren una integración específica. Si una herramienta no devuelve datos, el asistente debe indicarlo y no
inventar resultados.

## Cómo funciona (para la exposición)

```
Usuario ─► LLM ──(¿necesita herramienta?)── no ─► Respuesta final
             │ sí
             ▼
     {nombre, argumentos}  ─► tu código ejecuta la función real
             ▲                          │
             └──── resultado (observación) ◄──┘   (se repite hasta MAX_PASOS)
```

1. Se envían al LLM los mensajes **más la descripción JSON de las herramientas**.
2. El LLM responde con texto **o** con una petición de herramienta (`tool_calls` / `tool_use`).
3. El programa ejecuta la función y devuelve el resultado al modelo.
4. El modelo puede encadenar otra herramienta o dar la respuesta final.

## Seguridad incluida

- La calculadora usa `ast`, nunca `eval`.
- La base de datos se abre en modo **solo lectura** y solo acepta un `SELECT`.
- Límite de pasos (`MAX_PASOS`) para evitar bucles infinitos.
