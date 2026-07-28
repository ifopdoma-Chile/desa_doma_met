from app import create_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response

PREFIX = "/doma_met"

flask_app = create_app()
flask_app.config.update(
    APPLICATION_ROOT=PREFIX,
    SESSION_COOKIE_PATH=PREFIX,
)

flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)

application = DispatcherMiddleware(
    Response("Not Found wsgi", status=404),
    {PREFIX: flask_app}
)
