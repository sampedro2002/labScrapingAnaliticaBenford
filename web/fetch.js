// Se ejecuta dentro de la página de Instagram (main.py lo carga con page.evaluate),
// así las peticiones usan la sesión del navegador.
//
// Hace varios GET en paralelo (`workers` a la vez):
//   metaOnly = false: devuelve el JSON de cada respuesta (API).
//   metaOnly = true:  devuelve {meta: "..."} con el <meta name="description"> de la página
//                     de un perfil, y deja de descargar en cuanto lo encuentra.
// Si una petición falla devuelve {__status: código}.
async ([urls, appId, workers, metaOnly]) => {
    async function readMeta(response) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let text = "";
        while (true) {
            const {done, value} = await reader.read();
            if (done) return null;
            text += decoder.decode(value, {stream: true});
            const tag = text.match(/<meta[^>]*name="description"[^>]*>/);
            if (tag) {
                reader.cancel();
                return (tag[0].match(/content="([^"]*)"/) || [])[1] || null;
            }
        }
    }

    const results = new Array(urls.length);
    let next = 0;
    async function worker() {
        while (next < urls.length) {
            const i = next++;
            try {
                const response = await fetch(urls[i], {
                    credentials: "include",
                    headers: metaOnly ? {} : {"X-IG-App-ID": appId, "X-Requested-With": "XMLHttpRequest"},
                });
                if (!response.ok) results[i] = {__status: response.status};
                else results[i] = metaOnly ? {meta: await readMeta(response)} : await response.json();
            } catch (e) {
                results[i] = {__status: 0};
            }
        }
    }
    await Promise.all(Array.from({length: workers}, worker));
    return results;
}
