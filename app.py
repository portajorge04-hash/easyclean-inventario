from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from database import get_db, init_db, USE_PG, restaurar_desde_backup
from datetime import datetime, date, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import json
import os

app = Flask(__name__)
app.secret_key = 'easyclean_pro_2025_seguro'

# ─── Autenticación ────────────────────────────────────────────────────────────

@app.before_request
def verificar_sesion():
    rutas_publicas = {'login', 'static'}
    if request.endpoint and request.endpoint not in rutas_publicas:
        if 'user_id' not in session:
            return redirect(url_for('login'))

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('rol') != 'admin':
            flash('No tienes permisos para realizar esta acción.', 'danger')
            return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def es_admin():
    return session.get('rol') == 'admin'

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form['username'].strip().lower()
        password = request.form['password']
        db = get_db()
        user = db.execute(
            "SELECT * FROM usuarios WHERE username=? AND activo=1", (username,)
        ).fetchone()
        db.close()
        if user and check_password_hash(user['password_hash'], password):
            session.permanent = True
            session['user_id']  = user['id']
            session['username'] = user['username']
            session['nombre']   = user['nombre']
            session['rol']      = user['rol']
            return redirect(url_for('dashboard'))
        flash('Usuario o contraseña incorrectos.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── Gestión de usuarios (solo admin) ─────────────────────────────────────────

@app.route('/admin/usuarios', methods=['GET', 'POST'])
@admin_required
def admin_usuarios():
    db = get_db()
    if request.method == 'POST':
        accion = request.form.get('accion')
        if accion == 'nuevo':
            username = request.form['username'].strip().lower()
            nombre   = request.form['nombre'].strip()
            password = request.form['password']
            rol      = request.form.get('rol', 'viewer')
            existe = db.execute("SELECT id FROM usuarios WHERE username=?", (username,)).fetchone()
            if existe:
                flash(f'El usuario "{username}" ya existe.', 'warning')
            else:
                db.execute(
                    "INSERT INTO usuarios (username, password_hash, nombre, rol) VALUES (?,?,?,?)",
                    (username, generate_password_hash(password), nombre, rol)
                )
                registrar_log(db, 'Nuevo usuario', 'Usuarios', f'{username} ({rol})')
                db.commit()
                flash(f'Usuario "{username}" creado correctamente.', 'success')
        elif accion == 'toggle':
            uid = request.form['usuario_id']
            if int(uid) == session['user_id']:
                flash('No puedes desactivar tu propia cuenta.', 'warning')
            else:
                u = db.execute("SELECT username, activo FROM usuarios WHERE id=?", (uid,)).fetchone()
                db.execute("UPDATE usuarios SET activo = 1 - activo WHERE id=?", (uid,))
                estado = 'desactivado' if u['activo'] else 'activado'
                registrar_log(db, f'Usuario {estado}', 'Usuarios', u['username'])
                db.commit()
                flash('Estado del usuario actualizado.', 'info')
        elif accion == 'cambiar_password':
            uid       = request.form['usuario_id']
            nueva_pw  = request.form['nueva_password']
            u = db.execute("SELECT username FROM usuarios WHERE id=?", (uid,)).fetchone()
            db.execute("UPDATE usuarios SET password_hash=? WHERE id=?",
                       (generate_password_hash(nueva_pw), uid))
            registrar_log(db, 'Cambio contraseña', 'Usuarios', u['username'])
            db.commit()
            flash('Contraseña actualizada.', 'success')
        db.close()
        return redirect(url_for('admin_usuarios'))

    usuarios = db.execute("SELECT * FROM usuarios ORDER BY rol DESC, nombre").fetchall()
    db.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)

@app.context_processor
def inject_now():
    return {'now': datetime.now(), 'session': session, 'USE_PG': USE_PG}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def registrar_log(db, accion, modulo, descripcion):
    try:
        db.execute(
            "INSERT INTO log_actividad (usuario, accion, modulo, descripcion) VALUES (?,?,?,?)",
            (session.get('nombre', 'sistema'), accion, modulo, descripcion)
        )
    except Exception:
        pass  # nunca interrumpir la operación principal por un fallo de log

def generar_codigo_lote(producto_id, db):
    prod = db.execute("SELECT nombre, prefijo_lote FROM productos WHERE id=?", (producto_id,)).fetchone()
    # Usar prefijo personalizado si existe, si no tomar iniciales del nombre
    if prod['prefijo_lote']:
        prefijo = prod['prefijo_lote'].upper()
    else:
        palabras = prod['nombre'].split()
        prefijo = ''.join(p[0].upper() for p in palabras if p)[:3]
    hoy = datetime.now().strftime('%Y%m%d')
    ultimo = db.execute(
        "SELECT codigo_lote FROM lotes_produccion WHERE codigo_lote LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefijo}-{hoy}-%',)
    ).fetchone()
    seq = int(ultimo['codigo_lote'].split('-')[-1]) + 1 if ultimo else 1
    return f"{prefijo}-{hoy}-{seq:03d}"

def calcular_consumo(producto_id, num_lotes, db):
    ingredientes = db.execute("""
        SELECT mp.id, mp.nombre, mp.unidad as unidad_stock,
               fi.cantidad, fi.unidad
        FROM formula_ingredientes fi
        JOIN materias_primas mp ON mp.id = fi.materia_prima_id
        WHERE fi.producto_id = ?
    """, (producto_id,)).fetchall()

    consumo = []
    for ing in ingredientes:
        cant_total = ing['cantidad'] * num_lotes
        # Normalizar todo a la unidad del stock
        if ing['unidad'] == 'g' and ing['unidad_stock'] == 'kg':
            cant_stock = cant_total / 1000
        elif ing['unidad'] == 'ml' and ing['unidad_stock'] == 'kg':
            cant_stock = cant_total / 1000
        else:
            cant_stock = cant_total
        consumo.append({
            'mp_id': ing['id'],
            'nombre': ing['nombre'],
            'cantidad': round(cant_stock, 4),
            'unidad': ing['unidad_stock'],
            'cantidad_formula': ing['cantidad'],
            'unidad_formula': ing['unidad'],
        })
    return consumo

# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    db = get_db()
    productos = db.execute("SELECT * FROM productos").fetchall()

    mes_actual = datetime.now().strftime('%Y-%m')
    stats = {}
    for p in productos:
        ultimo = db.execute(
            "SELECT fecha_produccion, unidades_producidas FROM lotes_produccion WHERE producto_id=? ORDER BY id DESC LIMIT 1",
            (p['id'],)
        ).fetchone()
        total_mes = db.execute(
            "SELECT COALESCE(SUM(unidades_producidas),0) as total FROM lotes_produccion WHERE producto_id=? AND substr(fecha_produccion, 1, 7)=?",
            (p['id'], mes_actual)
        ).fetchone()['total']
        stats[p['id']] = {'ultimo': ultimo, 'total_mes': total_mes}

    # Alertas de stock bajo
    alertas_mp = db.execute(
        "SELECT nombre, stock_actual, stock_minimo, unidad FROM materias_primas WHERE stock_actual <= stock_minimo AND activo=1"
    ).fetchall()
    alertas_emp = db.execute(
        "SELECT te.nombre, te.stock_actual, te.stock_minimo FROM tipos_empaque te WHERE stock_actual <= stock_minimo"
    ).fetchall()

    # Últimos 5 lotes
    ultimos_lotes = db.execute("""
        SELECT lp.*, p.nombre as producto_nombre
        FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
        ORDER BY lp.id DESC LIMIT 5
    """).fetchall()

    # Producción últimos 7 días para gráfico
    hace_7 = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    datos_grafico = db.execute("""
        SELECT fecha_produccion, p.nombre as producto,
               SUM(unidades_producidas) as total
        FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
        WHERE fecha_produccion >= ?
        GROUP BY fecha_produccion, producto_id
        ORDER BY fecha_produccion
    """, (hace_7,)).fetchall()

    db.close()
    return render_template('dashboard.html',
        productos=productos, stats=stats,
        alertas_mp=alertas_mp, alertas_emp=alertas_emp,
        ultimos_lotes=ultimos_lotes,
        datos_grafico=json.dumps([dict(r) for r in datos_grafico])
    )

# ─── Producción ───────────────────────────────────────────────────────────────

@app.route('/produccion')
def produccion_lista():
    db = get_db()
    filtro_prod = request.args.get('producto', '')
    filtro_fecha = request.args.get('fecha', '')
    q = """
        SELECT lp.*, p.nombre as producto_nombre
        FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
        WHERE 1=1
    """
    params = []
    if filtro_prod:
        q += " AND lp.producto_id=?"; params.append(filtro_prod)
    if filtro_fecha:
        q += " AND lp.fecha_produccion=?"; params.append(filtro_fecha)
    q += " ORDER BY lp.id DESC"
    lotes = db.execute(q, params).fetchall()

    # Obtener operarios por lote
    lote_ops = {}
    for lote in lotes:
        ops = db.execute("""
            SELECT o.nombre FROM lote_operarios lo
            JOIN operarios o ON o.id=lo.operario_id
            WHERE lo.lote_id=?
        """, (lote['id'],)).fetchall()
        lote_ops[lote['id']] = [o['nombre'] for o in ops]

    productos = db.execute("SELECT * FROM productos").fetchall()
    db.close()
    return render_template('produccion_lista.html', lotes=lotes, lote_ops=lote_ops, productos=productos,
                           filtro_prod=filtro_prod, filtro_fecha=filtro_fecha)

@app.route('/produccion/nueva', methods=['GET', 'POST'])
def produccion_nueva():
    db = get_db()
    if request.method == 'POST':
        if not es_admin():
            flash('Solo los administradores pueden registrar lotes.', 'danger')
            db.close()
            return redirect(url_for('produccion_nueva'))
        producto_id = int(request.form['producto_id'])
        fecha = request.form['fecha_produccion']
        hora_inicio = request.form.get('hora_inicio', '')
        hora_fin = request.form.get('hora_fin', '')
        num_lotes = int(request.form.get('num_lotes', 1))
        unidades_producidas = request.form.get('unidades_producidas', '')
        unidades_ingresadas = request.form.get('unidades_ingresadas', '')
        estado = request.form.get('estado', 'Completado')
        observaciones = request.form.get('observaciones', '')
        operarios_ids = request.form.getlist('operarios')

        prod = db.execute("SELECT * FROM productos WHERE id=?", (producto_id,)).fetchone()
        if not unidades_producidas:
            unidades_producidas = prod['unidades_por_lote'] * num_lotes

        codigo = generar_codigo_lote(producto_id, db)

        db.execute("""
            INSERT INTO lotes_produccion
            (codigo_lote, producto_id, fecha_produccion, hora_inicio, hora_fin,
             num_lotes, unidades_producidas, unidades_ingresadas, estado, observaciones)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (codigo, producto_id, fecha, hora_inicio, hora_fin,
              num_lotes, unidades_producidas, unidades_ingresadas or None, estado, observaciones))

        lote_id = db.last_id()

        for op_id in operarios_ids:
            db.execute("INSERT OR IGNORE INTO lote_operarios VALUES (?,?)", (lote_id, op_id))

        # Descontar materia prima
        consumo = calcular_consumo(producto_id, num_lotes, db)
        for item in consumo:
            db.execute(
                "INSERT INTO lote_consumo_mp (lote_id, materia_prima_id, cantidad_usada, unidad) VALUES (?,?,?,?)",
                (lote_id, item['mp_id'], item['cantidad'], item['unidad'])
            )
            db.execute(
                "UPDATE materias_primas SET stock_actual = stock_actual - ? WHERE id=?",
                (item['cantidad'], item['mp_id'])
            )

        # Descontar empaques
        empaques_ids = request.form.getlist('empaques_ids')
        empaques_cant = request.form.getlist('empaques_cant')
        for emp_id, cant in zip(empaques_ids, empaques_cant):
            if cant and int(cant) > 0:
                db.execute(
                    "INSERT INTO lote_consumo_empaque (lote_id, tipo_empaque_id, cantidad_usada) VALUES (?,?,?)",
                    (lote_id, emp_id, int(cant))
                )
                db.execute(
                    "UPDATE tipos_empaque SET stock_actual = stock_actual - ? WHERE id=?",
                    (int(cant), emp_id)
                )

        registrar_log(db, 'Nuevo lote', 'Producción', f'Lote {codigo} — {prod["nombre"]} × {num_lotes}')
        db.commit()
        flash(f'Lote {codigo} registrado correctamente.', 'success')
        db.close()
        return redirect(url_for('produccion_lista'))

    productos = db.execute("SELECT * FROM productos").fetchall()
    operarios = db.execute("SELECT * FROM operarios WHERE activo=1 ORDER BY nombre").fetchall()
    hoy = date.today().isoformat()
    db.close()
    return render_template('produccion_nueva.html', productos=productos, operarios=operarios, hoy=hoy)

@app.route('/produccion/formula/<int:producto_id>/<int:num_lotes>')
def formula_json(producto_id, num_lotes):
    db = get_db()
    consumo = calcular_consumo(producto_id, num_lotes, db)
    prod = db.execute("SELECT * FROM productos WHERE id=?", (producto_id,)).fetchone()
    empaques = db.execute("SELECT * FROM tipos_empaque WHERE producto_id=?", (producto_id,)).fetchall()
    unidades = dict(prod)['unidades_por_lote'] * num_lotes
    db.close()
    return jsonify({
        'consumo': consumo,
        'unidades_estimadas': unidades,
        'empaques': [dict(e) for e in empaques]
    })

@app.route('/produccion/<int:lote_id>')
def produccion_detalle(lote_id):
    db = get_db()
    lote = db.execute("""
        SELECT lp.*, p.nombre as producto_nombre, p.tamano_envase_ml
        FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
        WHERE lp.id=?
    """, (lote_id,)).fetchone()
    if not lote:
        flash('Lote no encontrado.', 'danger')
        return redirect(url_for('produccion_lista'))

    operarios = db.execute("""
        SELECT o.nombre, o.cargo FROM lote_operarios lo
        JOIN operarios o ON o.id=lo.operario_id WHERE lo.lote_id=?
    """, (lote_id,)).fetchall()

    consumo_mp = db.execute("""
        SELECT mp.nombre, lc.cantidad_usada, lc.unidad
        FROM lote_consumo_mp lc JOIN materias_primas mp ON mp.id=lc.materia_prima_id
        WHERE lc.lote_id=?
    """, (lote_id,)).fetchall()

    consumo_emp = db.execute("""
        SELECT te.nombre, lc.cantidad_usada
        FROM lote_consumo_empaque lc JOIN tipos_empaque te ON te.id=lc.tipo_empaque_id
        WHERE lc.lote_id=?
    """, (lote_id,)).fetchall()

    db.close()
    return render_template('produccion_detalle.html', lote=lote, operarios=operarios,
                           consumo_mp=consumo_mp, consumo_emp=consumo_emp)

@app.route('/produccion/<int:lote_id>/editar', methods=['GET', 'POST'])
@admin_required
def editar_lote(lote_id):
    db = get_db()
    lote = db.execute("""
        SELECT lp.*, p.nombre as producto_nombre, p.unidades_por_lote as uds_por_lote
        FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
        WHERE lp.id=?
    """, (lote_id,)).fetchone()

    if not lote:
        flash('Lote no encontrado.', 'danger')
        db.close()
        return redirect(url_for('produccion_lista'))

    if request.method == 'POST':
        nueva_fecha       = request.form['fecha_produccion']
        nueva_hora_inicio = request.form.get('hora_inicio', '')
        nueva_hora_fin    = request.form.get('hora_fin', '')
        nuevo_num_lotes   = int(request.form.get('num_lotes', lote['num_lotes']))
        nuevas_unidades   = request.form.get('unidades_producidas') or lote['unidades_producidas']
        nuevas_uds_ing    = request.form.get('unidades_ingresadas') or None
        nuevo_estado      = request.form.get('estado', lote['estado'])
        nuevas_obs        = request.form.get('observaciones', '')
        nuevos_operarios  = request.form.getlist('operarios')

        # Ajuste de stock solo si cambió num_lotes
        if nuevo_num_lotes != lote['num_lotes']:
            # Revertir consumo de MP original
            for c in db.execute(
                "SELECT materia_prima_id, cantidad_usada FROM lote_consumo_mp WHERE lote_id=?",
                (lote_id,)
            ).fetchall():
                db.execute(
                    "UPDATE materias_primas SET stock_actual = stock_actual + ? WHERE id=?",
                    (c['cantidad_usada'], c['materia_prima_id'])
                )
            # Revertir consumo de empaques original
            for ce in db.execute(
                "SELECT tipo_empaque_id, cantidad_usada FROM lote_consumo_empaque WHERE lote_id=?",
                (lote_id,)
            ).fetchall():
                db.execute(
                    "UPDATE tipos_empaque SET stock_actual = stock_actual + ? WHERE id=?",
                    (ce['cantidad_usada'], ce['tipo_empaque_id'])
                )
            # Calcular y aplicar nuevo consumo de MP
            nuevo_consumo = calcular_consumo(lote['producto_id'], nuevo_num_lotes, db)
            db.execute("DELETE FROM lote_consumo_mp WHERE lote_id=?", (lote_id,))
            for item in nuevo_consumo:
                db.execute(
                    "INSERT INTO lote_consumo_mp (lote_id, materia_prima_id, cantidad_usada, unidad) VALUES (?,?,?,?)",
                    (lote_id, item['mp_id'], item['cantidad'], item['unidad'])
                )
                db.execute(
                    "UPDATE materias_primas SET stock_actual = stock_actual - ? WHERE id=?",
                    (item['cantidad'], item['mp_id'])
                )
            # Nuevo consumo de empaques desde el formulario
            empaques_ids  = request.form.getlist('empaques_ids')
            empaques_cant = request.form.getlist('empaques_cant')
            db.execute("DELETE FROM lote_consumo_empaque WHERE lote_id=?", (lote_id,))
            for emp_id, cant in zip(empaques_ids, empaques_cant):
                if cant and int(cant) > 0:
                    db.execute(
                        "INSERT INTO lote_consumo_empaque (lote_id, tipo_empaque_id, cantidad_usada) VALUES (?,?,?)",
                        (lote_id, emp_id, int(cant))
                    )
                    db.execute(
                        "UPDATE tipos_empaque SET stock_actual = stock_actual - ? WHERE id=?",
                        (int(cant), emp_id)
                    )

        # Actualizar campos del lote
        db.execute("""
            UPDATE lotes_produccion SET
                fecha_produccion=?, hora_inicio=?, hora_fin=?,
                num_lotes=?, unidades_producidas=?, unidades_ingresadas=?,
                estado=?, observaciones=?
            WHERE id=?
        """, (
            nueva_fecha, nueva_hora_inicio, nueva_hora_fin,
            nuevo_num_lotes, nuevas_unidades, nuevas_uds_ing,
            nuevo_estado, nuevas_obs, lote_id
        ))

        # Actualizar operarios
        db.execute("DELETE FROM lote_operarios WHERE lote_id=?", (lote_id,))
        for op_id in nuevos_operarios:
            db.execute("INSERT OR IGNORE INTO lote_operarios VALUES (?,?)", (lote_id, op_id))

        registrar_log(db, 'Editar lote', 'Producción',
                      f'Lote {lote["codigo_lote"]} editado por {session.get("nombre","")}')
        db.commit()
        flash(f'Lote {lote["codigo_lote"]} actualizado correctamente.', 'success')
        db.close()
        return redirect(url_for('produccion_lista'))

    # GET — cargar datos actuales
    operarios_lote_ids = [
        o['operario_id'] for o in db.execute(
            "SELECT operario_id FROM lote_operarios WHERE lote_id=?", (lote_id,)
        ).fetchall()
    ]
    todos_operarios = db.execute(
        "SELECT * FROM operarios WHERE activo=1 ORDER BY nombre"
    ).fetchall()
    empaques_lote = db.execute("""
        SELECT lce.tipo_empaque_id, lce.cantidad_usada, te.nombre
        FROM lote_consumo_empaque lce
        JOIN tipos_empaque te ON te.id=lce.tipo_empaque_id
        WHERE lce.lote_id=?
    """, (lote_id,)).fetchall()
    consumo_mp = db.execute("""
        SELECT mp.nombre, lc.cantidad_usada, lc.unidad
        FROM lote_consumo_mp lc JOIN materias_primas mp ON mp.id=lc.materia_prima_id
        WHERE lc.lote_id=?
    """, (lote_id,)).fetchall()

    db.close()
    return render_template('produccion_editar.html',
        lote=lote,
        operarios_lote_ids=operarios_lote_ids,
        todos_operarios=todos_operarios,
        empaques_lote=empaques_lote,
        consumo_mp=consumo_mp,
    )

# ─── Inventario Materias Primas ────────────────────────────────────────────────

@app.route('/inventario/mp')
def inventario_mp():
    db = get_db()
    mps = db.execute("SELECT * FROM materias_primas WHERE activo=1 ORDER BY nombre").fetchall()
    db.close()
    return render_template('inventario_mp.html', mps=mps)

@app.route('/inventario/mp/ajuste', methods=['POST'])
@admin_required
def ajuste_mp():
    db = get_db()
    mp_id = request.form['mp_id']
    nuevo_stock = float(request.form['nuevo_stock'])
    mp = db.execute("SELECT nombre FROM materias_primas WHERE id=?", (mp_id,)).fetchone()
    db.execute("UPDATE materias_primas SET stock_actual=? WHERE id=?", (nuevo_stock, mp_id))
    registrar_log(db, 'Ajuste stock', 'Inventario MP', f'{mp["nombre"]} → {nuevo_stock}')
    db.commit()
    flash('Stock actualizado.', 'success')
    db.close()
    return redirect(url_for('inventario_mp'))

@app.route('/inventario/mp/nueva', methods=['POST'])
@admin_required
def nueva_mp():
    db = get_db()
    nombre = request.form['nombre']
    unidad = request.form['unidad']
    stock_minimo = float(request.form.get('stock_minimo', 5))
    db.execute("INSERT INTO materias_primas (nombre, unidad, stock_minimo) VALUES (?,?,?)",
               (nombre, unidad, stock_minimo))
    registrar_log(db, 'Nueva MP', 'Inventario MP', f'{nombre} ({unidad})')
    db.commit()
    flash('Materia prima agregada.', 'success')
    db.close()
    return redirect(url_for('inventario_mp'))

# ─── Inventario Empaques ───────────────────────────────────────────────────────

@app.route('/inventario/empaques')
def inventario_empaques():
    db = get_db()
    empaques = db.execute("""
        SELECT te.*, p.nombre as producto_nombre
        FROM tipos_empaque te LEFT JOIN productos p ON p.id=te.producto_id
        ORDER BY p.nombre, te.nombre
    """).fetchall()
    productos = db.execute("SELECT * FROM productos").fetchall()
    db.close()
    return render_template('inventario_empaques.html', empaques=empaques, productos=productos)

@app.route('/inventario/empaques/ajuste', methods=['POST'])
@admin_required
def ajuste_empaque():
    db = get_db()
    emp_id = request.form['emp_id']
    nuevo_stock = int(request.form['nuevo_stock'])
    emp = db.execute("SELECT nombre FROM tipos_empaque WHERE id=?", (emp_id,)).fetchone()
    db.execute("UPDATE tipos_empaque SET stock_actual=? WHERE id=?", (nuevo_stock, emp_id))
    registrar_log(db, 'Ajuste stock', 'Empaques', f'{emp["nombre"]} → {nuevo_stock}')
    db.commit()
    flash('Stock de empaque actualizado.', 'success')
    db.close()
    return redirect(url_for('inventario_empaques'))

@app.route('/inventario/empaques/nuevo', methods=['POST'])
@admin_required
def nuevo_empaque():
    db = get_db()
    nombre = request.form['nombre']
    producto_id = request.form.get('producto_id') or None
    stock_minimo = int(request.form.get('stock_minimo', 100))
    db.execute("INSERT INTO tipos_empaque (nombre, producto_id, stock_minimo) VALUES (?,?,?)",
               (nombre, producto_id, stock_minimo))
    registrar_log(db, 'Nuevo empaque', 'Empaques', nombre)
    db.commit()
    flash('Empaque agregado.', 'success')
    db.close()
    return redirect(url_for('inventario_empaques'))

# ─── Compras MP ────────────────────────────────────────────────────────────────

@app.route('/compras/mp', methods=['GET', 'POST'])
def compras_mp():
    db = get_db()
    if request.method == 'POST':
        if not es_admin():
            flash('Solo los administradores pueden registrar compras.', 'danger')
            db.close()
            return redirect(url_for('compras_mp'))
        mp_id = request.form['materia_prima_id']
        fecha = request.form['fecha']
        cantidad = float(request.form['cantidad'])
        unidad = request.form['unidad']
        proveedor = request.form.get('proveedor', '')
        precio_u = request.form.get('precio_unitario') or None
        precio_t = request.form.get('precio_total') or None
        factura = request.form.get('numero_factura', '')
        obs = request.form.get('observaciones', '')

        db.execute("""
            INSERT INTO compras_mp (materia_prima_id, fecha, cantidad, unidad, proveedor,
            precio_unitario, precio_total, numero_factura, observaciones)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (mp_id, fecha, cantidad, unidad, proveedor, precio_u, precio_t, factura, obs))

        db.execute("UPDATE materias_primas SET stock_actual = stock_actual + ? WHERE id=?",
                   (cantidad, mp_id))
        mp_nombre = db.execute("SELECT nombre FROM materias_primas WHERE id=?", (mp_id,)).fetchone()['nombre']
        registrar_log(db, 'Nueva compra', 'Compras MP', f'{mp_nombre} — {cantidad} {unidad} — {fecha}')
        db.commit()
        flash('Compra registrada y stock actualizado.', 'success')
        db.close()
        return redirect(url_for('compras_mp'))

    mps = db.execute("SELECT * FROM materias_primas WHERE activo=1 ORDER BY nombre").fetchall()
    compras = db.execute("""
        SELECT c.*, mp.nombre as mp_nombre, mp.unidad as mp_unidad
        FROM compras_mp c JOIN materias_primas mp ON mp.id=c.materia_prima_id
        ORDER BY c.fecha DESC, c.id DESC LIMIT 50
    """).fetchall()
    hoy = date.today().isoformat()
    db.close()
    return render_template('compras_mp.html', mps=mps, compras=compras, hoy=hoy)

# ─── Compras Empaques ──────────────────────────────────────────────────────────

@app.route('/compras/empaques', methods=['GET', 'POST'])
def compras_empaques():
    db = get_db()
    if request.method == 'POST':
        if not es_admin():
            flash('Solo los administradores pueden registrar compras.', 'danger')
            db.close()
            return redirect(url_for('compras_empaques'))
        emp_id = request.form['tipo_empaque_id']
        fecha = request.form['fecha']
        cantidad = int(request.form['cantidad'])
        proveedor = request.form.get('proveedor', '')
        precio_u = request.form.get('precio_unitario') or None
        precio_t = request.form.get('precio_total') or None
        factura = request.form.get('numero_factura', '')
        obs = request.form.get('observaciones', '')

        db.execute("""
            INSERT INTO compras_empaque (tipo_empaque_id, fecha, cantidad, proveedor,
            precio_unitario, precio_total, numero_factura, observaciones)
            VALUES (?,?,?,?,?,?,?,?)
        """, (emp_id, fecha, cantidad, proveedor, precio_u, precio_t, factura, obs))

        db.execute("UPDATE tipos_empaque SET stock_actual = stock_actual + ? WHERE id=?",
                   (cantidad, emp_id))
        emp_nombre = db.execute("SELECT nombre FROM tipos_empaque WHERE id=?", (emp_id,)).fetchone()['nombre']
        registrar_log(db, 'Nueva compra', 'Compras Empaques', f'{emp_nombre} — {cantidad} uds — {fecha}')
        db.commit()
        flash('Compra de empaque registrada.', 'success')
        db.close()
        return redirect(url_for('compras_empaques'))

    empaques = db.execute("""
        SELECT te.*, p.nombre as producto_nombre
        FROM tipos_empaque te LEFT JOIN productos p ON p.id=te.producto_id
        ORDER BY p.nombre, te.nombre
    """).fetchall()
    compras = db.execute("""
        SELECT c.*, te.nombre as emp_nombre
        FROM compras_empaque c JOIN tipos_empaque te ON te.id=c.tipo_empaque_id
        ORDER BY c.fecha DESC, c.id DESC LIMIT 50
    """).fetchall()
    hoy = date.today().isoformat()
    db.close()
    return render_template('compras_empaques.html', empaques=empaques, compras=compras, hoy=hoy)

# ─── Operarios ────────────────────────────────────────────────────────────────

@app.route('/operarios', methods=['GET', 'POST'])
def operarios():
    db = get_db()
    if request.method == 'POST':
        if not es_admin():
            flash('Solo los administradores pueden modificar operarios.', 'danger')
            db.close()
            return redirect(url_for('operarios'))
        accion = request.form.get('accion')
        if accion == 'nuevo':
            nombre_op = request.form['nombre']
            db.execute("INSERT INTO operarios (nombre, cedula, cargo) VALUES (?,?,?)",
                       (nombre_op, request.form.get('cedula', ''), request.form.get('cargo', 'Operario')))
            registrar_log(db, 'Nuevo operario', 'Operarios', nombre_op)
            flash('Operario agregado.', 'success')
        elif accion == 'toggle':
            op_id = request.form['operario_id']
            op = db.execute("SELECT nombre, activo FROM operarios WHERE id=?", (op_id,)).fetchone()
            db.execute("UPDATE operarios SET activo = 1 - activo WHERE id=?", (op_id,))
            estado = 'desactivado' if op['activo'] else 'activado'
            registrar_log(db, f'Operario {estado}', 'Operarios', op['nombre'])
            flash('Estado del operario actualizado.', 'info')
        db.commit()
        db.close()
        return redirect(url_for('operarios'))

    ops = db.execute("SELECT * FROM operarios ORDER BY activo DESC, nombre").fetchall()
    db.close()
    return render_template('operarios.html', operarios=ops)

# ─── Reportes ─────────────────────────────────────────────────────────────────

@app.route('/reportes')
def reportes():
    db = get_db()
    mes = request.args.get('mes', datetime.now().strftime('%Y-%m'))

    produccion_mes = db.execute("""
        SELECT p.nombre as producto, COUNT(*) as lotes,
               SUM(lp.num_lotes) as total_lotes_prod,
               SUM(lp.unidades_producidas) as total_unidades,
               SUM(lp.unidades_ingresadas) as total_ingresadas
        FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
        WHERE substr(lp.fecha_produccion, 1, 7)=?
        GROUP BY lp.producto_id
    """, (mes,)).fetchall()

    consumo_mp_mes = db.execute("""
        SELECT mp.nombre, SUM(lc.cantidad_usada) as total, lc.unidad
        FROM lote_consumo_mp lc
        JOIN materias_primas mp ON mp.id=lc.materia_prima_id
        JOIN lotes_produccion lp ON lp.id=lc.lote_id
        WHERE substr(lp.fecha_produccion, 1, 7)=?
        GROUP BY lc.materia_prima_id, lc.unidad
        ORDER BY total DESC
    """, (mes,)).fetchall()

    compras_mp_mes = db.execute("""
        SELECT mp.nombre, SUM(c.cantidad) as total, c.unidad,
               SUM(c.precio_total) as costo
        FROM compras_mp c JOIN materias_primas mp ON mp.id=c.materia_prima_id
        WHERE substr(c.fecha, 1, 7)=?
        GROUP BY c.materia_prima_id
    """, (mes,)).fetchall()

    db.close()
    return render_template('reportes.html', mes=mes,
        produccion_mes=produccion_mes,
        consumo_mp_mes=consumo_mp_mes,
        compras_mp_mes=compras_mp_mes)

# ─── Productos ────────────────────────────────────────────────────────────────

@app.route('/productos')
def productos_lista():
    db = get_db()
    productos = db.execute("SELECT * FROM productos ORDER BY id").fetchall()
    producto_formulas = {}
    for p in productos:
        ingredientes = db.execute("""
            SELECT fi.id, mp.nombre, fi.cantidad, fi.unidad
            FROM formula_ingredientes fi
            JOIN materias_primas mp ON mp.id = fi.materia_prima_id
            WHERE fi.producto_id = ?
        """, (p['id'],)).fetchall()
        producto_formulas[p['id']] = ingredientes
    mps = db.execute("SELECT * FROM materias_primas WHERE activo=1 ORDER BY nombre").fetchall()
    db.close()
    return render_template('productos.html', productos=productos,
                           producto_formulas=producto_formulas, mps=mps)

@app.route('/productos/nuevo', methods=['POST'])
@admin_required
def producto_nuevo():
    db = get_db()
    nombre = request.form['nombre'].strip()
    tamano_envase = int(request.form['tamano_envase_ml'])
    litros_lote = float(request.form['litros_por_lote'])
    uds_raw = request.form.get('unidades_por_lote', '').strip()
    unidades_lote = int(uds_raw) if uds_raw else int((litros_lote * 1000) / tamano_envase)
    descripcion = request.form.get('descripcion', '')
    prefijo = request.form.get('prefijo_lote', '').strip().upper()

    db.execute("""
        INSERT INTO productos (nombre, tamano_envase_ml, litros_por_lote, unidades_por_lote, descripcion, prefijo_lote)
        VALUES (?,?,?,?,?,?)
    """, (nombre, tamano_envase, litros_lote, unidades_lote, descripcion, prefijo or None))
    prod_id = db.last_id()

    # Insertar ingredientes de la fórmula
    mp_ids = request.form.getlist('mp_id')
    cantidades = request.form.getlist('mp_cantidad')
    unidades = request.form.getlist('mp_unidad')

    for mp_id, cantidad, unidad in zip(mp_ids, cantidades, unidades):
        if mp_id and cantidad:
            db.execute("""
                INSERT INTO formula_ingredientes (producto_id, materia_prima_id, cantidad, unidad)
                VALUES (?,?,?,?)
            """, (prod_id, int(mp_id), float(cantidad), unidad))

    db.commit()
    flash(f'Producto "{nombre}" agregado con su fórmula.', 'success')
    db.close()
    return redirect(url_for('productos_lista'))

@app.route('/productos/<int:prod_id>/ingrediente/agregar', methods=['POST'])
@admin_required
def agregar_ingrediente(prod_id):
    db = get_db()
    mp_id = request.form['mp_id']
    cantidad = float(request.form['cantidad'])
    unidad = request.form['unidad']
    # Verificar si ya existe ese ingrediente en la fórmula
    existe = db.execute(
        "SELECT id FROM formula_ingredientes WHERE producto_id=? AND materia_prima_id=?",
        (prod_id, mp_id)
    ).fetchone()
    if existe:
        db.execute("UPDATE formula_ingredientes SET cantidad=?, unidad=? WHERE id=?",
                   (cantidad, unidad, existe['id']))
        flash('Ingrediente actualizado en la fórmula.', 'info')
    else:
        db.execute("""
            INSERT INTO formula_ingredientes (producto_id, materia_prima_id, cantidad, unidad)
            VALUES (?,?,?,?)
        """, (prod_id, mp_id, cantidad, unidad))
        flash('Ingrediente agregado a la fórmula.', 'success')
    db.commit()
    db.close()
    return redirect(url_for('productos_lista'))

@app.route('/productos/<int:prod_id>/ingrediente/<int:ing_id>/eliminar', methods=['POST'])
@admin_required
def eliminar_ingrediente(prod_id, ing_id):
    db = get_db()
    db.execute("DELETE FROM formula_ingredientes WHERE id=? AND producto_id=?", (ing_id, prod_id))
    db.commit()
    flash('Ingrediente eliminado.', 'info')
    db.close()
    return redirect(url_for('productos_lista'))

@app.route('/productos/<int:prod_id>/editar', methods=['POST'])
@admin_required
def editar_producto(prod_id):
    db = get_db()
    db.execute("""
        UPDATE productos SET nombre=?, tamano_envase_ml=?, litros_por_lote=?,
        unidades_por_lote=?, descripcion=?, prefijo_lote=? WHERE id=?
    """, (
        request.form['nombre'],
        int(request.form['tamano_envase_ml']),
        float(request.form['litros_por_lote']),
        int(request.form['unidades_por_lote']),
        request.form.get('descripcion', ''),
        request.form.get('prefijo_lote', '').upper() or None,
        prod_id
    ))
    db.commit()
    flash('Producto actualizado.', 'success')
    db.close()
    return redirect(url_for('productos_lista'))

# ─── Bodega ───────────────────────────────────────────────────────────────────

CATEGORIAS_BODEGA = ['Bolsas', 'Cepillos', 'Kits', 'Accesorios', 'General']

@app.route('/bodega')
def bodega():
    db = get_db()
    cat = request.args.get('categoria', '')
    q = "SELECT * FROM articulos_bodega WHERE activo=1"
    params = []
    if cat:
        q += " AND categoria=?"; params.append(cat)
    q += " ORDER BY categoria, nombre"
    articulos = db.execute(q, params).fetchall()

    alertas = db.execute(
        "SELECT COUNT(*) as n FROM articulos_bodega WHERE stock_actual <= stock_minimo AND activo=1"
    ).fetchone()['n']

    movimientos = db.execute("""
        SELECT m.*, a.nombre as articulo_nombre
        FROM movimientos_bodega m
        JOIN articulos_bodega a ON a.id = m.articulo_id
        ORDER BY m.id DESC LIMIT 30
    """).fetchall()

    categorias = db.execute(
        "SELECT DISTINCT categoria FROM articulos_bodega WHERE activo=1 ORDER BY categoria"
    ).fetchall()

    hoy = date.today().isoformat()
    db.close()
    return render_template('bodega.html', articulos=articulos, movimientos=movimientos,
                           alertas=alertas, categorias=categorias,
                           categorias_lista=CATEGORIAS_BODEGA,
                           cat_filtro=cat, hoy=hoy)

@app.route('/bodega/articulo/nuevo', methods=['POST'])
@admin_required
def bodega_articulo_nuevo():
    db = get_db()
    db.execute("""
        INSERT INTO articulos_bodega (nombre, categoria, unidad, stock_actual, stock_minimo, descripcion)
        VALUES (?,?,?,?,?,?)
    """, (
        request.form['nombre'],
        request.form.get('categoria', 'General'),
        request.form.get('unidad', 'und'),
        int(request.form.get('stock_actual', 0)),
        int(request.form.get('stock_minimo', 10)),
        request.form.get('descripcion', '')
    ))
    db.commit()
    flash(f'Artículo "{request.form["nombre"]}" agregado a bodega.', 'success')
    db.close()
    return redirect(url_for('bodega'))

@app.route('/bodega/entrada', methods=['POST'])
@admin_required
def bodega_entrada():
    db = get_db()
    art_id  = int(request.form['articulo_id'])
    cantidad = int(request.form['cantidad'])
    db.execute("""
        INSERT INTO movimientos_bodega
        (articulo_id, tipo, cantidad, fecha, motivo, referencia, responsable, observaciones)
        VALUES (?,?,?,?,?,?,?,?)
    """, (art_id, 'Entrada', cantidad,
          request.form['fecha'],
          request.form.get('motivo', 'Compra'),
          request.form.get('referencia', ''),
          request.form.get('responsable', ''),
          request.form.get('observaciones', '')))
    db.execute("UPDATE articulos_bodega SET stock_actual = stock_actual + ? WHERE id=?", (cantidad, art_id))
    art = db.execute("SELECT nombre FROM articulos_bodega WHERE id=?", (art_id,)).fetchone()
    registrar_log(db, 'Entrada bodega', 'Bodega', f'+{cantidad} {art["nombre"]}')
    db.commit()
    flash(f'Entrada registrada: +{cantidad} {art["nombre"]}.', 'success')
    db.close()
    return redirect(url_for('bodega'))

@app.route('/bodega/salida', methods=['POST'])
@admin_required
def bodega_salida():
    db = get_db()
    art_id  = int(request.form['articulo_id'])
    cantidad = int(request.form['cantidad'])
    art = db.execute("SELECT nombre, stock_actual FROM articulos_bodega WHERE id=?", (art_id,)).fetchone()
    if cantidad > art['stock_actual']:
        flash(f'Stock insuficiente. Solo hay {art["stock_actual"]} unidades de {art["nombre"]}.', 'danger')
        db.close()
        return redirect(url_for('bodega'))
    db.execute("""
        INSERT INTO movimientos_bodega
        (articulo_id, tipo, cantidad, fecha, motivo, referencia, responsable, observaciones)
        VALUES (?,?,?,?,?,?,?,?)
    """, (art_id, 'Salida', cantidad,
          request.form['fecha'],
          request.form.get('motivo', 'Uso'),
          request.form.get('referencia', ''),
          request.form.get('responsable', ''),
          request.form.get('observaciones', '')))
    db.execute("UPDATE articulos_bodega SET stock_actual = stock_actual - ? WHERE id=?", (cantidad, art_id))
    registrar_log(db, 'Salida bodega', 'Bodega', f'-{cantidad} {art["nombre"]}')
    db.commit()
    flash(f'Salida registrada: -{cantidad} {art["nombre"]}.', 'success')
    db.close()
    return redirect(url_for('bodega'))

@app.route('/bodega/ajuste', methods=['POST'])
@admin_required
def bodega_ajuste():
    db = get_db()
    art_id = int(request.form['articulo_id'])
    nuevo  = int(request.form['nuevo_stock'])
    art_n = db.execute("SELECT nombre FROM articulos_bodega WHERE id=?", (art_id,)).fetchone()['nombre']
    db.execute("UPDATE articulos_bodega SET stock_actual=? WHERE id=?", (nuevo, art_id))
    registrar_log(db, 'Ajuste bodega', 'Bodega', f'{art_n} → {nuevo}')
    db.commit()
    flash('Stock ajustado correctamente.', 'info')
    db.close()
    return redirect(url_for('bodega'))

# ─── Compras Bodega ───────────────────────────────────────────────────────────

@app.route('/compras/bodega', methods=['GET', 'POST'])
def compras_bodega():
    db = get_db()
    if request.method == 'POST':
        if not es_admin():
            flash('Solo los administradores pueden registrar compras.', 'danger')
            db.close()
            return redirect(url_for('compras_bodega'))
        art_id   = int(request.form['articulo_id'])
        fecha    = request.form['fecha']
        cantidad = int(request.form['cantidad'])
        proveedor = request.form.get('proveedor', '')
        precio_u  = request.form.get('precio_unitario') or None
        precio_t  = request.form.get('precio_total') or None
        factura   = request.form.get('numero_factura', '')
        obs       = request.form.get('observaciones', '')

        db.execute("""
            INSERT INTO compras_bodega
            (articulo_id, fecha, cantidad, proveedor, precio_unitario, precio_total, numero_factura, observaciones)
            VALUES (?,?,?,?,?,?,?,?)
        """, (art_id, fecha, cantidad, proveedor, precio_u, precio_t, factura, obs))

        db.execute("""
            INSERT INTO movimientos_bodega
            (articulo_id, tipo, cantidad, fecha, motivo, referencia, responsable, observaciones)
            VALUES (?,?,?,?,?,?,?,?)
        """, (art_id, 'Entrada', cantidad, fecha, 'Compra', factura, proveedor, obs))

        db.execute("UPDATE articulos_bodega SET stock_actual = stock_actual + ? WHERE id=?",
                   (cantidad, art_id))
        db.commit()
        art = db.execute("SELECT nombre FROM articulos_bodega WHERE id=?", (art_id,)).fetchone()
        registrar_log(db, 'Nueva compra', 'Compras Bodega', f'{art["nombre"]} — {cantidad} uds — {fecha}')
        db.commit()
        flash(f'Compra registrada: +{cantidad} {art["nombre"]}.', 'success')
        db.close()
        return redirect(url_for('compras_bodega'))

    articulos = db.execute(
        "SELECT * FROM articulos_bodega WHERE activo=1 ORDER BY categoria, nombre"
    ).fetchall()
    compras = db.execute("""
        SELECT cb.*, ab.nombre as art_nombre, ab.unidad as art_unidad
        FROM compras_bodega cb
        JOIN articulos_bodega ab ON ab.id = cb.articulo_id
        ORDER BY cb.fecha DESC, cb.id DESC LIMIT 50
    """).fetchall()
    hoy = date.today().isoformat()
    db.close()
    return render_template('compras_bodega.html', articulos=articulos, compras=compras, hoy=hoy)

# ─── Editar / Eliminar Compras ────────────────────────────────────────────────

@app.route('/compras/mp/<int:cid>/eliminar', methods=['POST'])
@admin_required
def eliminar_compra_mp(cid):
    db = get_db()
    c = db.execute("SELECT * FROM compras_mp WHERE id=?", (cid,)).fetchone()
    if c:
        db.execute("UPDATE materias_primas SET stock_actual = stock_actual - ? WHERE id=?",
                   (c['cantidad'], c['materia_prima_id']))
        mp_n = db.execute("SELECT nombre FROM materias_primas WHERE id=?", (c['materia_prima_id'],)).fetchone()['nombre']
        db.execute("DELETE FROM compras_mp WHERE id=?", (cid,))
        registrar_log(db, 'Eliminar compra', 'Compras MP', f'{mp_n} — {c["cantidad"]} {c["unidad"]} — {c["fecha"]}')
        db.commit()
        flash(f'Compra eliminada. Stock revertido en {c["cantidad"]} {c["unidad"]}.', 'info')
    db.close()
    return redirect(url_for('compras_mp'))

@app.route('/compras/mp/<int:cid>/editar', methods=['POST'])
@admin_required
def editar_compra_mp(cid):
    db = get_db()
    c = db.execute("SELECT * FROM compras_mp WHERE id=?", (cid,)).fetchone()
    if c:
        nueva_cant = float(request.form['cantidad'])
        diferencia  = nueva_cant - float(c['cantidad'])
        db.execute("""
            UPDATE compras_mp SET fecha=?, cantidad=?, proveedor=?,
            precio_unitario=?, precio_total=?, numero_factura=?, observaciones=?
            WHERE id=?
        """, (
            request.form['fecha'], nueva_cant,
            request.form.get('proveedor', ''),
            request.form.get('precio_unitario') or None,
            request.form.get('precio_total') or None,
            request.form.get('numero_factura', ''),
            request.form.get('observaciones', ''),
            cid
        ))
        if diferencia != 0:
            db.execute("UPDATE materias_primas SET stock_actual = stock_actual + ? WHERE id=?",
                       (diferencia, c['materia_prima_id']))
        mp_n = db.execute("SELECT nombre FROM materias_primas WHERE id=?", (c['materia_prima_id'],)).fetchone()['nombre']
        registrar_log(db, 'Editar compra', 'Compras MP', f'{mp_n} — cantidad: {c["cantidad"]}→{nueva_cant}')
        db.commit()
        flash('Compra de MP actualizada. Stock ajustado.', 'success')
    db.close()
    return redirect(url_for('compras_mp'))

@app.route('/compras/empaques/<int:cid>/eliminar', methods=['POST'])
@admin_required
def eliminar_compra_empaque(cid):
    db = get_db()
    c = db.execute("SELECT * FROM compras_empaque WHERE id=?", (cid,)).fetchone()
    if c:
        db.execute("UPDATE tipos_empaque SET stock_actual = stock_actual - ? WHERE id=?",
                   (c['cantidad'], c['tipo_empaque_id']))
        emp_n = db.execute("SELECT nombre FROM tipos_empaque WHERE id=?", (c['tipo_empaque_id'],)).fetchone()['nombre']
        db.execute("DELETE FROM compras_empaque WHERE id=?", (cid,))
        registrar_log(db, 'Eliminar compra', 'Compras Empaques', f'{emp_n} — {c["cantidad"]} uds — {c["fecha"]}')
        db.commit()
        flash(f'Compra eliminada. Stock de empaque revertido en {c["cantidad"]} uds.', 'info')
    db.close()
    return redirect(url_for('compras_empaques'))

@app.route('/compras/empaques/<int:cid>/editar', methods=['POST'])
@admin_required
def editar_compra_empaque(cid):
    db = get_db()
    c = db.execute("SELECT * FROM compras_empaque WHERE id=?", (cid,)).fetchone()
    if c:
        nueva_cant = int(request.form['cantidad'])
        diferencia  = nueva_cant - int(c['cantidad'])
        db.execute("""
            UPDATE compras_empaque SET fecha=?, cantidad=?, proveedor=?,
            precio_unitario=?, precio_total=?, numero_factura=?, observaciones=?
            WHERE id=?
        """, (
            request.form['fecha'], nueva_cant,
            request.form.get('proveedor', ''),
            request.form.get('precio_unitario') or None,
            request.form.get('precio_total') or None,
            request.form.get('numero_factura', ''),
            request.form.get('observaciones', ''),
            cid
        ))
        if diferencia != 0:
            db.execute("UPDATE tipos_empaque SET stock_actual = stock_actual + ? WHERE id=?",
                       (diferencia, c['tipo_empaque_id']))
        emp_n = db.execute("SELECT nombre FROM tipos_empaque WHERE id=?", (c['tipo_empaque_id'],)).fetchone()['nombre']
        registrar_log(db, 'Editar compra', 'Compras Empaques', f'{emp_n} — cantidad: {c["cantidad"]}→{nueva_cant}')
        db.commit()
        flash('Compra de empaque actualizada. Stock ajustado.', 'success')
    db.close()
    return redirect(url_for('compras_empaques'))

@app.route('/compras/bodega/<int:cid>/eliminar', methods=['POST'])
@admin_required
def eliminar_compra_bodega(cid):
    db = get_db()
    c = db.execute("SELECT * FROM compras_bodega WHERE id=?", (cid,)).fetchone()
    if c:
        db.execute("UPDATE articulos_bodega SET stock_actual = stock_actual - ? WHERE id=?",
                   (c['cantidad'], c['articulo_id']))
        db.execute("""
            INSERT INTO movimientos_bodega (articulo_id, tipo, cantidad, fecha, motivo, responsable)
            VALUES (?,?,?,?,?,?)
        """, (c['articulo_id'], 'Anulación', c['cantidad'],
              date.today().isoformat(), 'Anulación de compra', session.get('nombre', '')))
        art_n = db.execute("SELECT nombre FROM articulos_bodega WHERE id=?", (c['articulo_id'],)).fetchone()['nombre']
        db.execute("DELETE FROM compras_bodega WHERE id=?", (cid,))
        registrar_log(db, 'Eliminar compra', 'Compras Bodega', f'{art_n} — {c["cantidad"]} uds — {c["fecha"]}')
        db.commit()
        flash(f'Compra eliminada. Stock revertido en {c["cantidad"]} uds.', 'info')
    db.close()
    return redirect(url_for('compras_bodega'))

@app.route('/compras/bodega/<int:cid>/editar', methods=['POST'])
@admin_required
def editar_compra_bodega(cid):
    db = get_db()
    c = db.execute("SELECT * FROM compras_bodega WHERE id=?", (cid,)).fetchone()
    if c:
        nueva_cant = int(request.form['cantidad'])
        diferencia  = nueva_cant - int(c['cantidad'])
        db.execute("""
            UPDATE compras_bodega SET fecha=?, cantidad=?, proveedor=?,
            precio_unitario=?, precio_total=?, numero_factura=?, observaciones=?
            WHERE id=?
        """, (
            request.form['fecha'], nueva_cant,
            request.form.get('proveedor', ''),
            request.form.get('precio_unitario') or None,
            request.form.get('precio_total') or None,
            request.form.get('numero_factura', ''),
            request.form.get('observaciones', ''),
            cid
        ))
        if diferencia != 0:
            db.execute("UPDATE articulos_bodega SET stock_actual = stock_actual + ? WHERE id=?",
                       (diferencia, c['articulo_id']))
        art_n = db.execute("SELECT nombre FROM articulos_bodega WHERE id=?", (c['articulo_id'],)).fetchone()['nombre']
        registrar_log(db, 'Editar compra', 'Compras Bodega', f'{art_n} — cantidad: {c["cantidad"]}→{nueva_cant}')
        db.commit()
        flash('Compra de bodega actualizada. Stock ajustado.', 'success')
    db.close()
    return redirect(url_for('compras_bodega'))

# ─── Registro de Actividad ────────────────────────────────────────────────────

@app.route('/admin/actividad')
def admin_actividad():
    db = get_db()
    modulo = request.args.get('modulo', '')
    usuario = request.args.get('usuario', '')
    q = "SELECT * FROM log_actividad WHERE 1=1"
    params = []
    if modulo:
        q += " AND modulo=?"; params.append(modulo)
    if usuario:
        q += " AND usuario=?"; params.append(usuario)
    q += " ORDER BY id DESC LIMIT 200"
    logs = db.execute(q, params).fetchall()
    modulos = db.execute("SELECT DISTINCT modulo FROM log_actividad ORDER BY modulo").fetchall()
    usuarios = db.execute("SELECT DISTINCT usuario FROM log_actividad ORDER BY usuario").fetchall()
    db.close()
    return render_template('admin_actividad.html', logs=logs,
                           modulos=modulos, usuarios=usuarios,
                           filtro_modulo=modulo, filtro_usuario=usuario)

# ─── Admin / Respaldo ─────────────────────────────────────────────────────────

@app.route('/admin/restaurar-backup', methods=['POST'])
@admin_required
def admin_restaurar_backup():
    db = get_db()
    try:
        restaurar_desde_backup(db)
        registrar_log(db, 'Restaurar backup', 'Admin',
                      f'Restauración manual del backup 2026-05-04 por {session.get("nombre","")}')
        db.commit()
        flash('✅ Datos restaurados correctamente desde el backup del 2026-05-04.', 'success')
    except Exception as e:
        flash(f'❌ Error al restaurar: {e}', 'danger')
    finally:
        db.close()
    return redirect(url_for('admin_estado'))

@app.route('/admin/estado')
def admin_estado():
    db = get_db()
    try:
        tablas = {
            'productos':          db.execute("SELECT COUNT(*) as n FROM productos").fetchone()['n'],
            'materias_primas':    db.execute("SELECT COUNT(*) as n FROM materias_primas").fetchone()['n'],
            'lotes_produccion':   db.execute("SELECT COUNT(*) as n FROM lotes_produccion").fetchone()['n'],
            'compras_mp':         db.execute("SELECT COUNT(*) as n FROM compras_mp").fetchone()['n'],
            'compras_empaque':    db.execute("SELECT COUNT(*) as n FROM compras_empaque").fetchone()['n'],
            'compras_bodega':     db.execute("SELECT COUNT(*) as n FROM compras_bodega").fetchone()['n'],
            'articulos_bodega':   db.execute("SELECT COUNT(*) as n FROM articulos_bodega").fetchone()['n'],
            'movimientos_bodega': db.execute("SELECT COUNT(*) as n FROM movimientos_bodega").fetchone()['n'],
            'operarios':          db.execute("SELECT COUNT(*) as n FROM operarios").fetchone()['n'],
        }
        migraciones = db.execute(
            "SELECT nombre, aplicado_en FROM schema_migrations ORDER BY id"
        ).fetchall()
        ok = True
    except Exception as e:
        tablas = {}
        migraciones = []
        ok = False
    db.close()
    return render_template('admin_estado.html', tablas=tablas, migraciones=migraciones, ok=ok, use_pg=USE_PG)

@app.route('/admin/exportar')
def admin_exportar():
    db = get_db()
    def rows(query, params=()):
        return [dict(zip(r.keys(), r)) for r in db.execute(query, params).fetchall()]

    datos = {
        'exportado_en': datetime.now().isoformat(),
        'version': 'EasyClean Pro v1',
        'lotes_produccion': rows("""
            SELECT lp.*, p.nombre as producto_nombre
            FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
            ORDER BY lp.id
        """),
        'compras_mp': rows("""
            SELECT c.*, mp.nombre as mp_nombre
            FROM compras_mp c JOIN materias_primas mp ON mp.id=c.materia_prima_id
            ORDER BY c.id
        """),
        'compras_empaque': rows("""
            SELECT c.*, te.nombre as empaque_nombre
            FROM compras_empaque c JOIN tipos_empaque te ON te.id=c.tipo_empaque_id
            ORDER BY c.id
        """),
        'compras_bodega': rows("""
            SELECT cb.*, ab.nombre as articulo_nombre
            FROM compras_bodega cb JOIN articulos_bodega ab ON ab.id=cb.articulo_id
            ORDER BY cb.id
        """),
        'movimientos_bodega': rows("""
            SELECT m.*, a.nombre as articulo_nombre
            FROM movimientos_bodega m JOIN articulos_bodega a ON a.id=m.articulo_id
            ORDER BY m.id
        """),
        'stock_materias_primas': rows(
            "SELECT id, nombre, unidad, stock_actual, stock_minimo, activo FROM materias_primas ORDER BY nombre"
        ),
        'stock_empaques': rows("""
            SELECT te.id, te.nombre, te.stock_actual, te.stock_minimo, p.nombre as producto
            FROM tipos_empaque te LEFT JOIN productos p ON p.id=te.producto_id ORDER BY te.nombre
        """),
        'stock_bodega': rows(
            "SELECT id, nombre, categoria, unidad, stock_actual, stock_minimo FROM articulos_bodega WHERE activo=1 ORDER BY nombre"
        ),
        'operarios': rows("SELECT id, nombre, cedula, cargo, activo FROM operarios ORDER BY nombre"),
    }
    db.close()

    nombre_archivo = f'easyclean_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    from flask import Response
    return Response(
        json.dumps(datos, ensure_ascii=False, indent=2, default=str),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename={nombre_archivo}'}
    )

# ─── Startup ──────────────────────────────────────────────────────────────────
# Inicializar DB al arrancar (necesario para Gunicorn en Railway)
try:
    init_db()
except Exception as e:
    print(f"DB init warning: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5050)
