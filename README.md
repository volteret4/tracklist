# tracklist

Identifica el tracklist de un mix/set de DJ a partir de un enlace de YouTube:

1. Descarga el audio temporalmente con `yt-dlp` (se borra al terminar el análisis).
2. Recorre el audio a tramos (cada `TRACKLIST_STEP_SECONDS`, 45s por defecto) y
   reconoce cada tramo con `shazamio`.
3. Por cada tema identificado, vuelve a buscar con `yt-dlp` (`ytsearch1:`) el
   vídeo suelto de esa canción en YouTube.

Sin base de datos ni secretos — todo vive en memoria mientras dura el análisis
(`JOBS` en `app.py`), por eso conviene `restart: unless-stopped` pero no hace
falta persistir nada entre reinicios.

## Notas

- Los tramos sin match ("no match" en la respuesta de Shazam) son normales:
  transiciones, filtros/EQ del DJ o ediciones/bootleg sin huella en Shazam.
- Dos tramos consecutivos con el mismo resultado se colapsan en una sola fila
  (se queda el primer timestamp).
- La búsqueda de YouTube por tema puede devolver falsos positivos si el
  título/artista que da Shazam es ambiguo.
