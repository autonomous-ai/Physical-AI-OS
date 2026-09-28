# HTTP credentials and request logs

OS HTTP access logs include method, path, status, duration and client IP, but omit
the entire query string. This protects legacy `?token=<llm_api_key>` requests and
provisioning links containing other credentials. Recovery logs retain a stack
trace without dumping request headers, query strings or panic values.

Authentication is unchanged: session cookies, supported Bearer credentials and
the legacy `token` query parameter still work. After authentication, both HAL
proxies (`/api/hardware/*` and `/openapi.json`) remove every `token` query value
before forwarding. Other query arguments remain available to HAL.

Prefer cookies or Authorization headers over query credentials. This protection
does not erase historical logs or sanitize external reverse-proxy logs, browser
history, or application-specific log messages. Rotate credentials if previously
recorded logs were exposed.
