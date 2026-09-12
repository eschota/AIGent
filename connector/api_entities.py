"""The API entities the IDE can attach to a chat: one bridge per outside world.

Every entry names the endpoint that really does the work, so a dropped icon is a request the
agent can act on. `token` is what lands in the message text, `endpoint` is the API route the
agent (or the IDE) calls for that entity.
"""


def _icon(body, tint):
    return (f'<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false">'
            f'<circle cx="12" cy="12" r="11" fill="{tint}" opacity=".16"/>'
            f'{body}</svg>')


ENTITIES = (
    {"id": "youtube", "label": "YouTube", "endpoint": "/api/browser/youtube", "token": "[API:YouTube]",
     "hint": "YouTube через Chrome и computer-use: открыть, найти, загрузить, снять метрики",
     "tint": "#ff4d4d", "icon": _icon(
         '<path d="M10 8.6l6 3.4-6 3.4z" fill="#ff4d4d"/>'
         '<rect x="3.5" y="6" width="17" height="12" rx="3.4" fill="none" stroke="#ff4d4d" stroke-width="1.6"/>',
         "#ff4d4d")},
    {"id": "telegram", "label": "Telegram", "endpoint": "/api/sessions/{sid}/telegram",
     "token": "[API:Telegram]", "hint": "Telegram: отправить результат в канал или чат для валидации",
     "tint": "#4aa3f0", "icon": _icon(
         '<path d="M20 5.4L3.9 11.6l4.2 1.4 1.5 4.6 2.4-2.9 4.1 3z" fill="#4aa3f0"/>',
         "#4aa3f0")},
    {"id": "genvideo", "label": "GenVideo", "endpoint": "/api/gen/video", "token": "[API:GenVideo]",
     "hint": "GenVideo: собрать видео или анимацию из кадров и пресетов", "tint": "#a896ef",
     "icon": _icon('<path d="M4 7.5h9.5v9H4z" fill="none" stroke="#a896ef" stroke-width="1.6"/>'
                   '<path d="M14.5 12l5.5-3.4v6.8z" fill="#a896ef"/>', "#a896ef")},
    {"id": "genimage", "label": "GenImage", "endpoint": "/api/gen/image", "token": "[API:GenImage]",
     "hint": "GenImage: сгенерировать изображение, иконку или кадр презентации", "tint": "#79edd0",
     "icon": _icon('<rect x="3.5" y="5" width="17" height="14" rx="2.6" fill="none" '
                   'stroke="#79edd0" stroke-width="1.6"/><circle cx="9" cy="10" r="1.7" fill="#79edd0"/>'
                   '<path d="M5.5 17.5l4.6-4.4 3.2 3 2.6-2.4 3.6 3.4z" fill="#79edd0"/>', "#79edd0")},
)


def entities():
    """The strip the IDE renders: id, name, round icon and the endpoint behind it."""
    return [dict(item) for item in ENTITIES]
