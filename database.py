import os
import sqlite3
import re

DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_PG = bool(DATABASE_URL) and ('postgresql' in DATABASE_URL or DATABASE_URL.startswith('postgres'))

# ─── Wrappers unificados ──────────────────────────────────────────────────────

class UnifiedCursor:
    """Cursor que soporta acceso por nombre y por índice en ambas bases de datos."""
    def __init__(self, cursor, is_pg=False):
        self._cur = cursor
        self._pg = is_pg

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if self._pg and isinstance(row, dict):
            return _DictRow(row)
        return row

    def fetchall(self):
        rows = self._cur.fetchall()
        if self._pg:
            return [_DictRow(r) for r in rows]
        return rows

    def __iter__(self):
        for row in self._cur:
            if self._pg and isinstance(row, dict):
                yield _DictRow(row)
            else:
                yield row

class _DictRow:
    """Fila que soporta row['col'] y row[0]."""
    def __init__(self, data):
        self._data = dict(data)
        self._keys = list(self._data.keys())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._keys[key]]
        return self._data[key]

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._data.values())

class UnifiedDB:
    """Conexión unificada SQLite / PostgreSQL."""

    def __init__(self):
        if USE_PG:
            import psycopg2
            import psycopg2.extras
            url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
            self._conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
            self._pg = True
        else:
            self._conn = sqlite3.connect('easyclean.db')
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._pg = False

    def _adapt(self, query):
        if not self._pg:
            return query
        query = query.replace('?', '%s')
        # INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
        if re.search(r'INSERT\s+OR\s+IGNORE', query, re.IGNORECASE):
            query = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', query, flags=re.IGNORECASE)
            query = query.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
        # AUTOINCREMENT no existe en PostgreSQL
        query = query.replace('AUTOINCREMENT', '')
        return query

    def execute(self, query, params=()):
        query = self._adapt(query)
        if self._pg:
            cur = self._conn.cursor()
            cur.execute(query, params)
            return UnifiedCursor(cur, is_pg=True)
        else:
            return self._conn.execute(query, params)

    def executemany(self, query, params_list):
        query = self._adapt(query)
        if self._pg:
            cur = self._conn.cursor()
            for p in params_list:
                cur.execute(query, p)
        else:
            self._conn.executemany(query, params_list)

    def last_id(self):
        """Retorna el ID del último INSERT."""
        if self._pg:
            cur = self._conn.cursor()
            cur.execute("SELECT lastval()")
            row = cur.fetchone()
            return list(row.values())[0] if isinstance(row, dict) else row[0]
        else:
            return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

def get_db():
    return UnifiedDB()

# ─── Schemas ──────────────────────────────────────────────────────────────────

SCHEMA_SQLITE = '''
    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        tamano_envase_ml INTEGER NOT NULL,
        litros_por_lote REAL NOT NULL,
        unidades_por_lote INTEGER NOT NULL,
        descripcion TEXT,
        prefijo_lote TEXT
    );
    CREATE TABLE IF NOT EXISTS materias_primas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL UNIQUE,
        unidad TEXT NOT NULL,
        stock_actual REAL DEFAULT 0,
        stock_minimo REAL DEFAULT 5,
        proveedor TEXT,
        activo INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS formula_ingredientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER NOT NULL,
        materia_prima_id INTEGER NOT NULL,
        cantidad REAL NOT NULL,
        unidad TEXT NOT NULL,
        FOREIGN KEY (producto_id) REFERENCES productos(id),
        FOREIGN KEY (materia_prima_id) REFERENCES materias_primas(id)
    );
    CREATE TABLE IF NOT EXISTS tipos_empaque (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        producto_id INTEGER,
        stock_actual INTEGER DEFAULT 0,
        stock_minimo INTEGER DEFAULT 100,
        FOREIGN KEY (producto_id) REFERENCES productos(id)
    );
    CREATE TABLE IF NOT EXISTS operarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        cedula TEXT,
        cargo TEXT DEFAULT 'Operario',
        activo INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS lotes_produccion (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        codigo_lote TEXT UNIQUE NOT NULL,
        producto_id INTEGER NOT NULL,
        fecha_produccion TEXT NOT NULL,
        hora_inicio TEXT,
        hora_fin TEXT,
        num_lotes INTEGER DEFAULT 1,
        unidades_producidas INTEGER,
        unidades_ingresadas INTEGER,
        estado TEXT DEFAULT 'Completado',
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (producto_id) REFERENCES productos(id)
    );
    CREATE TABLE IF NOT EXISTS lote_operarios (
        lote_id INTEGER NOT NULL,
        operario_id INTEGER NOT NULL,
        PRIMARY KEY (lote_id, operario_id),
        FOREIGN KEY (lote_id) REFERENCES lotes_produccion(id),
        FOREIGN KEY (operario_id) REFERENCES operarios(id)
    );
    CREATE TABLE IF NOT EXISTS lote_consumo_mp (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lote_id INTEGER NOT NULL,
        materia_prima_id INTEGER NOT NULL,
        cantidad_usada REAL NOT NULL,
        unidad TEXT NOT NULL,
        FOREIGN KEY (lote_id) REFERENCES lotes_produccion(id),
        FOREIGN KEY (materia_prima_id) REFERENCES materias_primas(id)
    );
    CREATE TABLE IF NOT EXISTS lote_consumo_empaque (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lote_id INTEGER NOT NULL,
        tipo_empaque_id INTEGER NOT NULL,
        cantidad_usada INTEGER NOT NULL,
        FOREIGN KEY (lote_id) REFERENCES lotes_produccion(id),
        FOREIGN KEY (tipo_empaque_id) REFERENCES tipos_empaque(id)
    );
    CREATE TABLE IF NOT EXISTS compras_mp (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        materia_prima_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        cantidad REAL NOT NULL,
        unidad TEXT NOT NULL,
        proveedor TEXT,
        precio_unitario REAL,
        precio_total REAL,
        numero_factura TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (materia_prima_id) REFERENCES materias_primas(id)
    );
    CREATE TABLE IF NOT EXISTS compras_empaque (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo_empaque_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        proveedor TEXT,
        precio_unitario REAL,
        precio_total REAL,
        numero_factura TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tipo_empaque_id) REFERENCES tipos_empaque(id)
    );
    CREATE TABLE IF NOT EXISTS articulos_bodega (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        categoria TEXT DEFAULT 'General',
        unidad TEXT DEFAULT 'und',
        stock_actual INTEGER DEFAULT 0,
        stock_minimo INTEGER DEFAULT 10,
        descripcion TEXT,
        activo INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS movimientos_bodega (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        articulo_id INTEGER NOT NULL,
        tipo TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        motivo TEXT,
        referencia TEXT,
        responsable TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (articulo_id) REFERENCES articulos_bodega(id)
    );
    CREATE TABLE IF NOT EXISTS compras_bodega (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        articulo_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        proveedor TEXT,
        precio_unitario REAL,
        precio_total REAL,
        numero_factura TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (articulo_id) REFERENCES articulos_bodega(id)
    );
    CREATE TABLE IF NOT EXISTS schema_migrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL UNIQUE,
        aplicado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        nombre TEXT NOT NULL,
        rol TEXT DEFAULT 'viewer',
        activo INTEGER DEFAULT 1,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
'''

SCHEMA_PG = '''
    CREATE TABLE IF NOT EXISTS productos (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL,
        tamano_envase_ml INTEGER NOT NULL,
        litros_por_lote REAL NOT NULL,
        unidades_por_lote INTEGER NOT NULL,
        descripcion TEXT,
        prefijo_lote TEXT
    );
    CREATE TABLE IF NOT EXISTS materias_primas (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL UNIQUE,
        unidad TEXT NOT NULL,
        stock_actual REAL DEFAULT 0,
        stock_minimo REAL DEFAULT 5,
        proveedor TEXT,
        activo INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS formula_ingredientes (
        id SERIAL PRIMARY KEY,
        producto_id INTEGER NOT NULL,
        materia_prima_id INTEGER NOT NULL,
        cantidad REAL NOT NULL,
        unidad TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tipos_empaque (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL,
        producto_id INTEGER,
        stock_actual INTEGER DEFAULT 0,
        stock_minimo INTEGER DEFAULT 100
    );
    CREATE TABLE IF NOT EXISTS operarios (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL,
        cedula TEXT,
        cargo TEXT DEFAULT 'Operario',
        activo INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS lotes_produccion (
        id SERIAL PRIMARY KEY,
        codigo_lote TEXT UNIQUE NOT NULL,
        producto_id INTEGER NOT NULL,
        fecha_produccion TEXT NOT NULL,
        hora_inicio TEXT,
        hora_fin TEXT,
        num_lotes INTEGER DEFAULT 1,
        unidades_producidas INTEGER,
        unidades_ingresadas INTEGER,
        estado TEXT DEFAULT 'Completado',
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS lote_operarios (
        lote_id INTEGER NOT NULL,
        operario_id INTEGER NOT NULL,
        PRIMARY KEY (lote_id, operario_id)
    );
    CREATE TABLE IF NOT EXISTS lote_consumo_mp (
        id SERIAL PRIMARY KEY,
        lote_id INTEGER NOT NULL,
        materia_prima_id INTEGER NOT NULL,
        cantidad_usada REAL NOT NULL,
        unidad TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lote_consumo_empaque (
        id SERIAL PRIMARY KEY,
        lote_id INTEGER NOT NULL,
        tipo_empaque_id INTEGER NOT NULL,
        cantidad_usada INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS compras_mp (
        id SERIAL PRIMARY KEY,
        materia_prima_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        cantidad REAL NOT NULL,
        unidad TEXT NOT NULL,
        proveedor TEXT,
        precio_unitario REAL,
        precio_total REAL,
        numero_factura TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS compras_empaque (
        id SERIAL PRIMARY KEY,
        tipo_empaque_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        proveedor TEXT,
        precio_unitario REAL,
        precio_total REAL,
        numero_factura TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS articulos_bodega (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL,
        categoria TEXT DEFAULT 'General',
        unidad TEXT DEFAULT 'und',
        stock_actual INTEGER DEFAULT 0,
        stock_minimo INTEGER DEFAULT 10,
        descripcion TEXT,
        activo INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS movimientos_bodega (
        id SERIAL PRIMARY KEY,
        articulo_id INTEGER NOT NULL,
        tipo TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        motivo TEXT,
        referencia TEXT,
        responsable TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS compras_bodega (
        id SERIAL PRIMARY KEY,
        articulo_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        proveedor TEXT,
        precio_unitario REAL,
        precio_total REAL,
        numero_factura TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS schema_migrations (
        id SERIAL PRIMARY KEY,
        nombre TEXT NOT NULL UNIQUE,
        aplicado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        nombre TEXT NOT NULL,
        rol TEXT DEFAULT 'viewer',
        activo INTEGER DEFAULT 1,
        creado_en TEXT DEFAULT CURRENT_TIMESTAMP
    );
'''

# ─── Migraciones de datos ────────────────────────────────────────────────────
# Cada migración se ejecuta UNA SOLA VEZ y queda registrada en schema_migrations.
# Los datos ya cargados por el equipo NUNCA se borran ni se duplican.

def _has_migration(db, nombre):
    try:
        row = db.execute(
            "SELECT id FROM schema_migrations WHERE nombre=?", (nombre,)
        ).fetchone()
        return row is not None
    except Exception:
        return False

def _record_migration(db, nombre):
    db.execute(
        "INSERT OR IGNORE INTO schema_migrations (nombre) VALUES (?)", (nombre,)
    )

def _mig_v001_datos_iniciales(db):
    """Productos, materias primas, fórmulas y empaques iniciales."""
    n = db.execute("SELECT COUNT(*) as n FROM productos").fetchone()['n']
    if n > 0:
        return  # Ya hay datos, no tocar nada

    db.executemany(
        "INSERT INTO productos (nombre, tamano_envase_ml, litros_por_lote, unidades_por_lote, descripcion, prefijo_lote) VALUES (?,?,?,?,?,?)",
        [
            ('Suelas y Sintéticos', 120, 80, 666, 'Limpiador para suelas y materiales sintéticos', 'SS'),
            ('Material Textil', 160, 80, 500, 'Limpiador para telas y materiales textiles', 'MT'),
            ('Icon White (Suelas Amarillas)', 120, 14.985, 100, 'Blanqueador especial para suelas amarillas', 'IW'),
        ]
    )
    db.commit()

    mps = [
        ('Sal', 'kg', 0, 5),
        ('LESS', 'kg', 0, 20),
        ('Agua', 'kg', 0, 100),
        ('DBL', 'kg', 0, 10),
        ('Poliacrilato de Sodio', 'g', 0, 500),
        ('Conservante', 'kg', 0, 1),
        ('LESS @ 28%', 'kg', 0, 10),
        ('Betaína', 'kg', 0, 5),
        ('Glicerina', 'kg', 0, 5),
        ('Propilenglicol', 'kg', 0, 3),
        ('EDTA', 'g', 0, 300),
        ('Peróxido de Hidrógeno al 50%', 'kg', 0, 5),
        ('Goma Xantan', 'g', 0, 500),
        ('Nonilfenol', 'g', 0, 400),
    ]
    db.executemany(
        "INSERT INTO materias_primas (nombre, unidad, stock_actual, stock_minimo) VALUES (?,?,?,?)",
        mps
    )
    db.commit()

    prod_rows = db.execute("SELECT id, nombre FROM productos").fetchall()
    mp_rows   = db.execute("SELECT id, nombre FROM materias_primas").fetchall()
    prod = {r['nombre']: r['id'] for r in prod_rows}
    mp   = {r['nombre']: r['id'] for r in mp_rows}

    formulas = [
        (prod['Suelas y Sintéticos'], mp['Sal'], 2.17, 'kg'),
        (prod['Suelas y Sintéticos'], mp['LESS'], 17.38, 'kg'),
        (prod['Suelas y Sintéticos'], mp['Agua'], 54.27, 'kg'),
        (prod['Suelas y Sintéticos'], mp['DBL'], 6.19, 'kg'),
        (prod['Suelas y Sintéticos'], mp['Poliacrilato de Sodio'], 120, 'g'),
        (prod['Suelas y Sintéticos'], mp['Conservante'], 120, 'g'),
        (prod['Material Textil'], mp['LESS @ 28%'], 7.91, 'kg'),
        (prod['Material Textil'], mp['Agua'], 58.90, 'kg'),
        (prod['Material Textil'], mp['Betaína'], 4.74, 'kg'),
        (prod['Material Textil'], mp['DBL'], 4.34, 'kg'),
        (prod['Material Textil'], mp['Glicerina'], 1.58, 'kg'),
        (prod['Material Textil'], mp['Propilenglicol'], 2.37, 'kg'),
        (prod['Material Textil'], mp['Conservante'], 158, 'g'),
        (prod['Material Textil'], mp['EDTA'], 160, 'g'),
        (prod['Material Textil'], mp['Poliacrilato de Sodio'], 77, 'g'),
        (prod['Icon White (Suelas Amarillas)'], mp['Peróxido de Hidrógeno al 50%'], 4.5, 'kg'),
        (prod['Icon White (Suelas Amarillas)'], mp['Glicerina'], 600, 'g'),
        (prod['Icon White (Suelas Amarillas)'], mp['Goma Xantan'], 450, 'g'),
        (prod['Icon White (Suelas Amarillas)'], mp['Nonilfenol'], 300, 'g'),
        (prod['Icon White (Suelas Amarillas)'], mp['Conservante'], 10, 'ml'),
        (prod['Icon White (Suelas Amarillas)'], mp['Agua'], 9.135, 'kg'),
    ]
    db.executemany(
        "INSERT INTO formula_ingredientes (producto_id, materia_prima_id, cantidad, unidad) VALUES (?,?,?,?)",
        formulas
    )

    empaques = [
        ('Envase 120ml (Suelas)', prod['Suelas y Sintéticos'], 0, 500),
        ('Subtapa (Suelas)', prod['Suelas y Sintéticos'], 0, 500),
        ('Tapa (Suelas)', prod['Suelas y Sintéticos'], 0, 500),
        ('Etiqueta Suelas y Sintéticos', prod['Suelas y Sintéticos'], 0, 500),
        ('Envase 160ml (Textil)', prod['Material Textil'], 0, 300),
        ('Etiqueta Material Textil', prod['Material Textil'], 0, 300),
        ('Envase 120ml (Icon White)', prod['Icon White (Suelas Amarillas)'], 0, 100),
        ('Etiqueta Icon White', prod['Icon White (Suelas Amarillas)'], 0, 100),
    ]
    db.executemany(
        "INSERT INTO tipos_empaque (nombre, producto_id, stock_actual, stock_minimo) VALUES (?,?,?,?)",
        empaques
    )
    db.commit()

def _mig_v002_articulos_bodega(db):
    """Artículos iniciales de bodega — solo inserta si no existen."""
    articulos = [
        ('Bolsa de Kit',      'Bolsas',   'und', 50),
        ('Bolsa de Boutique', 'Bolsas',   'und', 50),
        ('Bolsa de Celofán',  'Bolsas',   'und', 100),
        ('Cepillo de Suelas', 'Cepillos', 'und', 20),
        ('Cepillo de Tela',   'Cepillos', 'und', 20),
    ]
    for nombre, cat, unidad, minimo in articulos:
        existe = db.execute(
            "SELECT id FROM articulos_bodega WHERE nombre=?", (nombre,)
        ).fetchone()
        if not existe:
            db.execute(
                "INSERT INTO articulos_bodega (nombre, categoria, unidad, stock_actual, stock_minimo) VALUES (?,?,?,?,?)",
                (nombre, cat, unidad, 0, minimo)
            )

def _mig_v003_usuarios(db):
    """Crea el usuario administrador inicial."""
    from werkzeug.security import generate_password_hash
    existe = db.execute("SELECT id FROM usuarios WHERE username=?", ('admin',)).fetchone()
    if not existe:
        db.execute(
            "INSERT INTO usuarios (username, password_hash, nombre, rol) VALUES (?,?,?,?)",
            ('admin', generate_password_hash('EasyClean2025!'), 'Administrador', 'admin')
        )

# ─── Lista maestra de migraciones ────────────────────────────────────────────
# Para agregar cambios futuros: añadir una nueva tupla al final de esta lista.
# NUNCA modificar ni eliminar las entradas existentes.

MIGRACIONES = [
    ('v001_datos_iniciales',  _mig_v001_datos_iniciales),
    ('v002_articulos_bodega', _mig_v002_articulos_bodega),
    ('v003_usuarios',         _mig_v003_usuarios),
]

# ─── Inicialización ───────────────────────────────────────────────────────────

def init_db():
    db = get_db()

    # 1. Crear / actualizar tablas (siempre seguro con IF NOT EXISTS)
    schema = SCHEMA_PG if USE_PG else SCHEMA_SQLITE
    for stmt in schema.split(';'):
        stmt = stmt.strip()
        if stmt:
            try:
                db.execute(stmt)
            except Exception as e:
                print(f'Schema warning: {e}')
    db.commit()

    # 2. Ejecutar solo las migraciones que aún no se han aplicado
    for nombre, fn in MIGRACIONES:
        if not _has_migration(db, nombre):
            try:
                fn(db)
                _record_migration(db, nombre)
                db.commit()
                print(f'Migración aplicada: {nombre}')
            except Exception as e:
                print(f'Error en migración {nombre}: {e}')

    db.close()
    print("Base de datos inicializada correctamente.")

if __name__ == '__main__':
    init_db()
