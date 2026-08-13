# tracklist

Identifica el tracklist de un mix/set de DJ a partir de un enlace de YouTube:

1. Descarga el audio temporalmente con `yt-dlp` (se borra al terminar el análisis).
2. Recorre el audio a tramos (cada `TRACKLIST_STEP_SECONDS`, 45s por defecto) y
   reconoce cada tramo con `shazamio`.
3. Por cada tema identificado, vuelve a buscar con `yt-dlp` (`ytsearch1:`,
   con `extract_flat` para no disparar el bloqueo anti-bot) el vídeo suelto
   de esa canción en YouTube.
4. Al terminar, guarda el tracklist completo en `tracklist.db` (SQLite) para
   consultarlo después desde el "Historial" de la propia web, sin tener que
   reanalizar el mismo mix. Se borra solo desde ahí (botón "Borrar"), nunca
   automáticamente.

Mientras un análisis está en curso, su progreso vive en memoria (`JOBS` en
`app.py`) — si el contenedor se reinicia a mitad de un análisis se pierde ese
análisis en curso, pero el historial ya guardado en `tracklist.db` no se ve
afectado.

## Cookies (bloqueo anti-bot de YouTube)

Si `yt-dlp` falla con `Sign in to confirm you're not a bot` (frecuente desde
IPs de servidor), rellena `cookies.txt` en este directorio con cookies de una
sesión de YouTube logueada, en formato Netscape:

1. Instala una extensión tipo [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   en un navegador con sesión iniciada en YouTube.
2. Exporta las cookies de `youtube.com` a un archivo.
3. Copia ese archivo como `services/tracklist/cookies.txt` (se monta como
   solo lectura en el contenedor — no hace falta rebuild, solo
   `docker compose restart tracklist`).

`cookies.txt` está en `.gitignore` — nunca se sube al repo. Vacío por
defecto (yt-dlp funciona igual, sin cookies, hasta que YouTube empiece a
pedir verificación).

## Notas

- Los tramos sin match ("no match" en la respuesta de Shazam) son normales:
  transiciones, filtros/EQ del DJ o ediciones/bootleg sin huella en Shazam.
- Dos tramos consecutivos con el mismo resultado se colapsan en una sola fila
  (se queda el primer timestamp).
- La búsqueda de YouTube por tema puede devolver falsos positivos si el
  título/artista que da Shazam es ambiguo.
