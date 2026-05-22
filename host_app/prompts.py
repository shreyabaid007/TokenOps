"""Demo prompts and system contexts for the four host_app endpoints.

Consumed by:
  - host_app.main when an endpoint receives a request (SYSTEM_PROMPTS provide
    the role context wrapped around user input before forwarding to the proxy)
  - scripts/seed_demo_traffic.py to generate varied demo traffic (PROMPTS
    supply realistic per-endpoint examples; the seed script mixes in slight
    rephrasings to demonstrate the semantic cache)
"""


SYSTEM_PROMPTS: dict[str, str] = {
    "sql-analyst": (
        "You are a senior data analyst. The user asks a question about the company's "
        "data warehouse. Respond with the SQL query that answers it, then a one-line "
        "explanation. Assume standard tables: orders, customers, products, events. "
        "Be concise."
    ),
    "code-reviewer": (
        "You are a senior software engineer reviewing a code snippet. Identify bugs, "
        "security issues, performance problems, or style violations. Be specific and "
        "brief — three findings at most."
    ),
    "log-explainer": (
        "You are an on-call engineer. The user pastes a log line, stack trace, or "
        "error message. Explain what likely went wrong and the most probable cause "
        "in two or three sentences, then suggest the first thing to check."
    ),
    "doc-writer": (
        "You are a Python documentation writer. Given an undocumented function, "
        "produce a PEP 257-style docstring. Include Args, Returns, and Raises "
        "sections only when relevant. Keep it tight."
    ),
}


PROMPTS: dict[str, list[str]] = {
    "sql-analyst": [
        "What was total revenue by region last quarter?",
        "Show me the top 10 customers by lifetime value.",
        "How many active users did we have each month in 2024?",
        "Which products had the largest year-over-year revenue growth?",
        "What is the average order value by acquisition channel?",
        "List all orders over $5,000 placed in the last 30 days.",
        "Compare conversion rates between mobile and desktop traffic.",
        "Which customer segment has the highest repeat purchase rate?",
    ],
    "code-reviewer": [
        "def divide(a, b): return a / b",
        "for user in users: user.last_login = datetime.now()",
        "def get_user(id): return db.query(f\"SELECT * FROM users WHERE id={id}\")",
        "if password == request.form['password']: login_user(user)",
        "async def fetch_all(urls): return [requests.get(u) for u in urls]",
        "data = open('config.json').read(); config = json.loads(data)",
        "def is_admin(user): return user.role == 'admin' or user.email.endswith('@admin.com')",
        "def cache(fn):\n    saved = {}\n    def inner(x): return saved.setdefault(x, fn(x))\n    return inner",
    ],
    "log-explainer": [
        "2026-05-19T14:22:01Z ERROR psycopg2.OperationalError: server closed the connection unexpectedly",
        "TypeError: Cannot read property 'map' of undefined at UserList.render (UserList.js:42)",
        "HTTP 429 Too Many Requests — retry-after: 60 — endpoint: /api/v1/embeddings",
        "Traceback (most recent call last):\n  File 'app.py', line 87, in handler\n    result = process(data)\nKeyError: 'user_id'",
        "WARN [Kafka] Producer flush timed out after 30000ms; 247 messages in buffer",
        "OOMKilled: container exceeded memory limit 512Mi at PID 1 (python app.py)",
        "HTTP 502 Bad Gateway — upstream connect timeout (2s) — service: payments-api",
        "ssl.SSLCertVerificationError: certificate verify failed: unable to get local issuer certificate (api.stripe.com)",
    ],
    "doc-writer": [
        "def slugify(s): return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')",
        "def chunk(lst, n): return [lst[i:i+n] for i in range(0, len(lst), n)]",
        "def retry(fn, attempts=3):\n    for _ in range(attempts):\n        try: return fn()\n        except: continue",
        "def merge_intervals(intervals):\n    intervals.sort()\n    merged = []\n    for s, e in intervals:\n        if merged and s <= merged[-1][1]:\n            merged[-1][1] = max(merged[-1][1], e)\n        else:\n            merged.append([s, e])\n    return merged",
        "def days_between(a, b): return (b - a).days",
        "def is_palindrome(s):\n    s = ''.join(c.lower() for c in s if c.isalnum())\n    return s == s[::-1]",
        "def memoize(fn):\n    cache = {}\n    def wrapped(*args):\n        if args not in cache:\n            cache[args] = fn(*args)\n        return cache[args]\n    return wrapped",
        "def flatten(xs):\n    out = []\n    for x in xs:\n        if isinstance(x, list): out.extend(flatten(x))\n        else: out.append(x)\n    return out",
    ],
}
