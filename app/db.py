import psycopg2

DATABASE_CONFIG = {
    'host': 'giscc.ifop.cl',
    'port': 5432,
    'dbname': 'gisdb',
    'user': 'appmovil',
    'password': '2025$Doma##'
}

def get_db_connection():
    try:
        return psycopg2.connect(**DATABASE_CONFIG)
    except Exception:
        return None