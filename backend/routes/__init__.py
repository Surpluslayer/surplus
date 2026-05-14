"""
HTTP routes — one router per mechanism stage.

    events    01      POST /events,                GET /events/{id}
    pipeline  02-03   POST /events/{id}/run,        GET /events/{id}/prospects
    matching  04      POST /events/{id}/match,      GET /events/{id}/matches
    roi       05      GET  /events/{id}/roi
"""
