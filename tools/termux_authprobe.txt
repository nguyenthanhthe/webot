import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/data/data/com.termux/files/home/wv-bot")
from wvbot.http_client import HttpClient  # noqa: E402

OUT = "/data/data/com.termux/files/usr/tmp/probe_out.txt"
fh = open(OUT, "w")


def log(*a):
    fh.write(" ".join(str(x) for x in a) + "\n")
    fh.flush()


http = HttpClient(timeout=20, max_retries=1)

try:
    log("--- signIn with dummy credentials ---")
    try:
        data = http.request(
            "POST",
            "https://auth.api-wolvesville.com/players/signInWithEmailAndPassword",
            {"email": "nobody@example.invalid", "password": "wrong-password"},
            {},
        )
        log("sign-in ->", json.dumps(data)[:400])
    except urllib.error.HTTPError as exc:
        log("sign-in -> HTTP", exc.code, exc.read()[:400])
    except Exception as exc:
        log("sign-in -> ERR", type(exc).__name__, exc)

    log("--- createIdToken with dummy refresh token ---")
    try:
        data = http.request(
            "POST",
            "https://auth.api-wolvesville.com/players/createIdToken",
            {"refreshToken": "dummy-refresh-token"},
            {},
        )
        log("createIdToken ->", json.dumps(data)[:400])
    except urllib.error.HTTPError as exc:
        log("createIdToken -> HTTP", exc.code, exc.read()[:400])
    except Exception as exc:
        log("createIdToken -> ERR", type(exc).__name__, exc)

    log("--- custom game list with dummy bearer ---")
    try:
        data = http.request(
            "GET",
            "https://game.api-wolvesville.com/api/public/game/custom?language=en",
            None,
            {"Authorization": "Bearer dummy-token"},
        )
        log("custom list ->", json.dumps(data)[:400])
    except urllib.error.HTTPError as exc:
        log("custom list -> HTTP", exc.code, exc.read()[:400])
    except Exception as exc:
        log("custom list -> ERR", type(exc).__name__, exc)
except Exception:
    import traceback
    log("FATAL", traceback.format_exc())
finally:
    fh.close()
