from flask import Flask

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'doma_met_2025_ifop'
    app.config['TEMPLATES_AUTO_RELOAD'] = True

    from app.routes import main
    app.register_blueprint(main)

    return app
