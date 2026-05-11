import os
import sqlite3
import re

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
_url_lower = DATABASE_URL.lower()
USE_PG = bool(DATABASE_URL) and (
    _url_lower.startswith('postgresql') or
    _url_lower.startswith('postgres:')
) and not DATABASE_URL.startswith('${{')

print(f"[DB] DATABASE_URL configurada: {'Sí' if DATABASE_URL else 'No'}")
if DATABASE_URL:
    print(f"[DB] Primeros 30 chars: {DATABASE_URL[:30]!r}")
print(f"[DB] Motor seleccionado: {'PostgreSQL' if USE_PG else 'SQLite (local)'}")

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
    CREATE TABLE IF NOT EXISTS log_actividad (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT,
        accion TEXT NOT NULL,
        modulo TEXT NOT NULL,
        descripcion TEXT,
        fecha_hora TEXT DEFAULT CURRENT_TIMESTAMP
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
    CREATE TABLE IF NOT EXISTS log_actividad (
        id SERIAL PRIMARY KEY,
        usuario TEXT,
        accion TEXT NOT NULL,
        modulo TEXT NOT NULL,
        descripcion TEXT,
        fecha_hora TEXT DEFAULT CURRENT_TIMESTAMP
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

def _mig_v004_restore_backup_20260504(db):
    """Restaura el backup del 2026-05-04: compras, stocks y operarios."""

    # ── Stock de Materias Primas ─────────────────────────────────────────────
    # Valores exactos del backup exportado el 2026-05-04
    stock_mp = [
        (1,   0.0),   # Sal
        (2,  50.0),   # LESS
        (3,   0.0),   # Agua
        (4,  53.0),   # DBL
        (5,   0.0),   # Poliacrilato de Sodio
        (6,   0.0),   # Conservante
        (7,  50.0),   # LESS @ 28%
        (8,  30.0),   # Betaína
        (9,  20.0),   # Glicerina
        (10, 20.0),   # Propilenglicol
        (11,  0.0),   # EDTA
        (12,  0.0),   # Peróxido de Hidrógeno al 50%
        (13,  0.0),   # Goma Xantan
        (14,  0.0),   # Nonilfenol
    ]
    for mp_id, stock in stock_mp:
        db.execute("UPDATE materias_primas SET stock_actual=? WHERE id=?", (stock, mp_id))

    # ── Stock de Empaques ────────────────────────────────────────────────────
    stock_empaques = [
        (1,   0),   # Envase 120ml (Suelas)
        (2,   0),   # Subtapa (Suelas)
        (3,   0),   # Tapa (Suelas)
        (4,   0),   # Etiqueta Suelas y Sintéticos
        (5,   0),   # Envase 160ml (Textil)
        (6,   0),   # Etiqueta Material Textil
        (7, 120),   # Envase 120ml (Icon White)
        (8,   0),   # Etiqueta Icon White
    ]
    for emp_id, stock in stock_empaques:
        db.execute("UPDATE tipos_empaque SET stock_actual=? WHERE id=?", (stock, emp_id))

    # ── Stock de Bodega ──────────────────────────────────────────────────────
    stock_bodega = [
        (1, 0),   # Bolsa de Kit
        (2, 0),   # Bolsa de Boutique
        (3, 0),   # Bolsa de Celofán
        (4, 0),   # Cepillo de Suelas
        (5, 0),   # Cepillo de Tela
    ]
    for art_id, stock in stock_bodega:
        db.execute("UPDATE articulos_bodega SET stock_actual=? WHERE id=?", (stock, art_id))

    # ── Operarios ────────────────────────────────────────────────────────────
    operarios = [
        ('YOSETH PORTA',  '1045771471', 'Jefe de Producción', 1),
        ('KEINER GARCIA', '1043443183', 'Operario',            1),
    ]
    for nombre, cedula, cargo, activo in operarios:
        existe = db.execute(
            "SELECT id FROM operarios WHERE cedula=?", (cedula,)
        ).fetchone()
        if not existe:
            db.execute(
                "INSERT OR IGNORE INTO operarios (nombre, cedula, cargo, activo) VALUES (?,?,?,?)",
                (nombre, cedula, cargo, activo)
            )

    # ── Compras de Materias Primas ───────────────────────────────────────────
    # Solo restaurar si la tabla está vacía (evita duplicados)
    n_compras_mp = db.execute("SELECT COUNT(*) as n FROM compras_mp").fetchone()['n']
    if n_compras_mp == 0:
        compras_mp = [
            (4,  '2026-04-27', 53.0, 'kg', 'UNIQUIMICOS SAS',  6386.0,  338487.0, 'FAC-UNIQ-001', ''),
            (8,  '2026-04-27', 30.0, 'kg', 'UNIQUIMICOS SAS',  7563.0,  226890.0, 'FAC-UNIQ-002', ''),
            (9,  '2026-04-27', 20.0, 'kg', 'UNIQUIMICOS SAS', 10924.0,  218487.0, 'FAC-UNIQ-003', ''),
            (10, '2026-04-27', 20.0, 'kg', 'UNIQUIMICOS SAS', 11008.0,  220168.0, 'FAC-UNIQ-004', ''),
            (2,  '2026-04-27', 50.0, 'kg', 'UNIQUIMICOS SAS',  5462.0,  546218.0, 'FAC-UNIQ-005', ''),
            (7,  '2026-04-27', 50.0, 'kg', 'UNIQUIMICOS SAS',  5462.0,  546218.0, 'FAC-UNIQ-006', ''),
        ]
        for mp_id, fecha, cantidad, unidad, prov, pu, pt, factura, obs in compras_mp:
            db.execute("""
                INSERT INTO compras_mp
                (materia_prima_id, fecha, cantidad, unidad, proveedor,
                 precio_unitario, precio_total, numero_factura, observaciones)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (mp_id, fecha, cantidad, unidad, prov, pu, pt, factura, obs))

    # ── Compras de Empaques ──────────────────────────────────────────────────
    n_compras_emp = db.execute("SELECT COUNT(*) as n FROM compras_empaque").fetchone()['n']
    if n_compras_emp == 0:
        db.execute("""
            INSERT INTO compras_empaque
            (tipo_empaque_id, fecha, cantidad, proveedor,
             precio_unitario, precio_total, numero_factura, observaciones)
            VALUES (?,?,?,?,?,?,?,?)
        """, (7, '2026-04-30', 120, 'ENVASES DUQUE SALDARRIAGA.SAS.',
              738655.0, 105585.0, 'FAC-ENVDUQ-001', ''))


MIGRACIONES = [
    ('v001_datos_iniciales',  _mig_v001_datos_iniciales),
    ('v002_articulos_bodega', _mig_v002_articulos_bodega),
    ('v003_usuarios',         _mig_v003_usuarios),
    ('v004_restore_backup_20260504', _mig_v004_restore_backup_20260504),
]

# ─── Guardia de datos (corre en CADA arranque) ────────────────────────────────

def _guardia_de_datos(db):
    """
    Se ejecuta en CADA inicio del servidor.
    Detecta si los datos del backup fueron borrados y los restaura automáticamente.
    No duplica: usa facturas como huella digital para saber si los datos ya existen.
    """
    try:
        # Huella: si FAC-UNIQ-001 no existe, las compras fueron borradas
        fac_existe = db.execute(
            "SELECT COUNT(*) as n FROM compras_mp WHERE numero_factura='FAC-UNIQ-001'"
        ).fetchone()['n']

        if fac_existe == 0:
            print("[GUARDIA] Compras MP faltantes — restaurando datos del backup...")
            _mig_v004_restore_backup_20260504(db)
            db.commit()
            print("[GUARDIA] Datos restaurados correctamente.")

        # Siempre garantizar que los operarios existen (son críticos para el sistema)
        for cedula, nombre, cargo in [
            ('1045771471', 'YOSETH PORTA',  'Jefe de Producción'),
            ('1043443183', 'KEINER GARCIA', 'Operario'),
        ]:
            existe = db.execute(
                "SELECT id FROM operarios WHERE cedula=?", (cedula,)
            ).fetchone()
            if not existe:
                db.execute(
                    "INSERT OR IGNORE INTO operarios (nombre, cedula, cargo, activo) VALUES (?,?,?,?)",
                    (nombre, cedula, cargo, 1)
                )
                db.commit()
                print(f"[GUARDIA] Operario restaurado: {nombre}")

        # Garantizar usuario admin si fue eliminado
        try:
            admin = db.execute(
                "SELECT id FROM usuarios WHERE username='admin'"
            ).fetchone()
            if not admin:
                from werkzeug.security import generate_password_hash
                db.execute(
                    "INSERT OR IGNORE INTO usuarios (username, password_hash, nombre, rol) VALUES (?,?,?,?)",
                    ('admin', generate_password_hash('EasyClean2025!'), 'Administrador', 'admin')
                )
                db.commit()
                print("[GUARDIA] Usuario admin restaurado.")
        except Exception:
            pass

    except Exception as e:
        print(f"[GUARDIA] Error en guardia de datos: {e}")


def restaurar_desde_backup(db):
    """
    Restauración manual completa desde el backup del 2026-05-04.
    Borra compras existentes y vuelve a insertar los datos del backup.
    Solo para uso desde el panel de administración.
    """
    # Borrar compras actuales (para reinsertar limpias desde backup)
    db.execute("DELETE FROM compras_mp")
    db.execute("DELETE FROM compras_empaque")
    db.commit()

    # Ejecutar la migración de restauración
    _mig_v004_restore_backup_20260504(db)
    db.commit()
    print("[RESTAURAR] Restauración manual completada desde backup 2026-05-04.")


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

    # 3. Guardia de datos: corre en cada arranque, restaura si detecta pérdida
    _guardia_de_datos(db)

    db.close()
    print("Base de datos inicializada correctamente.")

if __name__ == '__main__':
    init_db()
