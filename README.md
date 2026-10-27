# Análisis de personalidad de perfiles de Instagram con IA

Programa de línea de comandos en Python que extrae los datos públicos de un perfil de Instagram y los envía a la API de **Gemini** para inferir rasgos de personalidad con el modelo **Big Five (OCEAN)**. Proyecto del laboratorio de Scraping (Arquitectura, semestre 7).

El resultado es un `.json` con los datos extraídos, un `.txt` con el análisis y las fotos de los seguidores. Para verlo, abre `web/index.html` en el navegador y elige el JSON.

## Cómo funciona

1. **Extracción** (ver [más abajo](#extracción-de-datos)): perfil, seguidores, seguidos, publicaciones y comentarios.
2. **Engagement**: likes y comentarios totales, promedio, tasa sobre seguidores, mejores publicaciones y varianza.
3. **Análisis con Gemini**: todo lo anterior se arma como contexto y se envía al modelo.
4. **Salida**: se guardan el JSON completo y el texto del análisis.

## Análisis con IA

### Configuración

La clave y el modelo se leen del `.env`:

```env
GEMINI_API_KEY=tu_api_key_de_gemini_aqui   # https://aistudio.google.com/apikey
GEMINI_MODEL=gemini-3.1-flash-lite         # opcional (es el valor por defecto)
```

Si el modelo configurado falla (por ejemplo, error 404 `no longer available` porque Google lo retiró), el programa prueba los modelos de `FALLBACK_MODELS` en orden. Para usar otro modelo, cambia `GEMINI_MODEL`; no hace falta tocar el código.

### Qué se le envía al modelo

| Señal | Detalle |
|---|---|
| Perfil | Usuario, bio, cantidad de seguidores, seguidos y publicaciones |
| Red | Hasta 100 usernames de seguidores y seguidos (`MAX_USERS`) |
| Engagement | Métricas calculadas (sin repetir el top 5, para ahorrar tokens) |
| Publicaciones | Hasta 20, con fecha, likes, comentarios, vistas y caption (máx. 1000 caracteres) |
| Comentarios | Autor y texto de cada comentario de las publicaciones |

### Qué devuelve

Un análisis breve en español, con un nivel (Alta / Media / Baja) y una línea de evidencia por rasgo:

```
**Apertura**: Alta — Publica sobre viajes y proyectos creativos variados.
**Responsabilidad**: Media — Publica con regularidad pero sin una línea editorial fija.
**Extraversión**: Alta — Muchas publicaciones con gente y comentarios de amigos.
**Amabilidad**: Alta — Responde con cercanía y recibe comentarios afectuosos.
**Neuroticismo**: Baja — Tono estable, sin mensajes de angustia.

**Nota de confianza**: Inferencia conductual a partir de señales públicas, no una evaluación clínica.
```

(Ejemplo ilustrativo.)

### Cómo está construido

- El prompt y la instrucción de sistema (`PROMPT`, `SYSTEM_INSTRUCTION`) están al inicio de `main.py`; ahí se ajusta el formato o el modelo de personalidad.
- `temperature` es 0.3 para que las respuestas sean estables entre ejecuciones.
- Orden de modelos: `GEMINI_MODEL` (o `gemini-3.1-flash-lite`), luego `gemini-3.8-flash` (`FALLBACK_MODELS`). Si uno falla, se intenta el siguiente.
- La llamada es una petición HTTP directa a la API REST de Gemini (sin SDK), con la clave en el encabezado `x-goog-api-key`.
- Si el modelo termina por una causa distinta de `STOP` (por ejemplo, límite de tokens) se muestra una advertencia.
- Si la llamada falla, el JSON igualmente se guarda y no se crea el `.txt`.

### Limitaciones

- Es una **inferencia** a partir de lo que la persona publica; no es un test psicométrico ni un diagnóstico.
- Con pocos datos (pocas publicaciones o comentarios) la confianza es baja.
- Los comentarios son de terceros y pueden sesgar la lectura del perfil.
- Los datos del perfil se envían a un servicio externo (Google): úsalo solo con cuentas que tengas autorización para analizar.

## Extracción de datos

Se usa Chrome (Playwright) solo para tener una sesión iniciada en Instagram; los datos se piden en paralelo
(`WORKERS` peticiones a la vez) desde la página:

| Dato | Fuente |
|---|---|
| Perfil (bio, seguidores, seguidos, publicaciones) | Página del perfil |
| Publicaciones | Scroll de la grilla del perfil para los shortcodes + API `media/{id}/info` |
| Comentarios | API `media/{id}/comments` (hasta `COMMENTS_PER_POST` por publicación) |
| Seguidores / seguidos | API `friendships/{id}/followers` y `following` (50 por página) |
| Foto y conteos de cada seguidor | Foto: URL que da la API. Conteos: el `meta description` de la página de cada seguidor |

Si Instagram responde 429 (demasiadas peticiones) se reintenta con pausas de 15, 30, 60 y 120 s.

Constantes al inicio de `main.py`:

| Constante | Por defecto | Qué controla |
|---|---|---|
| `LIMIT` | 200 | Máximo de seguidores, seguidos y publicaciones |
| `COMMENTS_PER_POST` | 10 | Comentarios por publicación (máx. ~15; 0 para no pedirlos) |
| `WORKERS` | 4 | Peticiones simultáneas: más es más rápido, pero aumenta el riesgo de bloqueo |

Con `LIMIT = 200` una ejecución completa tarda unos 2 minutos.

## Instalación y uso

Requisitos: Python 3.11+, Google Chrome, una cuenta de Instagram y una API key de Gemini.

```powershell
py -m venv scrap
.\scrap\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env   # y completa los valores
python main.py <usuario>
```

`scrap/` es el entorno virtual (no se sube a Git). Si no pasas el usuario como argumento, el programa lo pide.

Las cookies de Instagram van en el `.env` como variables separadas. Cópialas desde DevTools
(F12 → Application → Cookies → instagram.com). Trata `sessionid` como una contraseña.

```env
SESSIONID=...
DS_USER_ID=...
CSRFTOKEN=...
```

La sesión también queda guardada en el perfil de Chrome (`.chrome_profile/`); puedes iniciar sesión
a mano en la ventana que abre el programa.

## Salida

| Archivo | Contenido |
|---|---|
| `<usuario>_data.json` | Perfil, `followers_list`, `following_list`, `publications`, `seguidores_detalle` (foto, publicaciones, seguidores y seguidos de cada seguidor), `engagement`, `benford` y `analysis` |
| `<usuario>_analisis.txt` | Solo el análisis de Gemini |
| `fotos/<seguidor>.jpg` | Foto de perfil de cada seguidor |

## Estructura

| Archivo | Función |
|---|---|
| `main.py` | Todo el programa: scraping, engagement, análisis con Gemini y guardado |
| `web/fetch.js` | Función que corre dentro de Chrome y hace las peticiones en paralelo |
| `web/index.html` | Abre un `<usuario>_data.json` y muestra la gráfica de Benford y el resto de los datos |
| `.env.example` | Plantilla de cookies de Instagram y clave de Gemini |
| `requirements.txt` | Dependencias |

## Notas

- Instagram cambia sus endpoints con frecuencia; algunas secciones pueden salir vacías.
- Pedir muchos datos puede provocar límites o bloqueos en la cuenta.
- No subas a Git `.env`, `.chrome_profile/`, `fotos/`, `scrap/`, `*_data.json` ni `*_analisis.txt` (ya están en `.gitignore`). `.env.example` sí se sube, porque es solo la plantilla.
- Proyecto académico: el scraping puede ir contra los Términos de Uso de Instagram y los datos pueden ser personales. Úsalo con responsabilidad.

## Licencia

[MIT](LICENSE)
