from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from database import get_db, init_db
from datetime import datetime, date
import json
import os

app = Flask(__name__)
app.secret_key = 'easyclean_pro_2025'

@app.context_processor
def inject_now():
    return {'now': datetime.now()}

# ─── Helpers ──────────────────────────────────────────────────────────────────

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

    stats = {}
    for p in productos:
        ultimo = db.execute(
            "SELECT fecha_produccion, unidades_producidas FROM lotes_produccion WHERE producto_id=? ORDER BY id DESC LIMIT 1",
            (p['id'],)
        ).fetchone()
        total_mes = db.execute(
            "SELECT COALESCE(SUM(unidades_producidas),0) as total FROM lotes_produccion WHERE producto_id=? AND strftime('%Y-%m', fecha_produccion)=strftime('%Y-%m','now')",
            (p['id'],)
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
    datos_grafico = db.execute("""
        SELECT fecha_produccion, p.nombre as producto,
               SUM(unidades_producidas) as total
        FROM lotes_produccion lp JOIN productos p ON p.id=lp.producto_id
        WHERE fecha_produccion >= date('now','-7 days')
        GROUP BY fecha_produccion, producto_id
        ORDER BY fecha_produccion
    """).fetchall()

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

# ─── Inventario Materias Primas ────────────────────────────────────────────────

@app.route('/inventario/mp')
def inventario_mp():
    db = get_db()
    mps = db.execute("SELECT * FROM materias_primas WHERE activo=1 ORDER BY nombre").fetchall()
    db.close()
    return render_template('inventario_mp.html', mps=mps)

@app.route('/inventario/mp/ajuste', methods=['POST'])
def ajuste_mp():
    db = get_db()
    mp_id = request.form['mp_id']
    nuevo_stock = float(request.form['nuevo_stock'])
    db.execute("UPDATE materias_primas SET stock_actual=? WHERE id=?", (nuevo_stock, mp_id))
    db.commit()
    flash('Stock actualizado.', 'success')
    db.close()
    return redirect(url_for('inventario_mp'))

@app.route('/inventario/mp/nueva', methods=['POST'])
def nueva_mp():
    db = get_db()
    nombre = request.form['nombre']
    unidad = request.form['unidad']
    stock_minimo = float(request.form.get('stock_minimo', 5))
    db.execute("INSERT INTO materias_primas (nombre, unidad, stock_minimo) VALUES (?,?,?)",
               (nombre, unidad, stock_minimo))
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
def ajuste_empaque():
    db = get_db()
    emp_id = request.form['emp_id']
    nuevo_stock = int(request.form['nuevo_stock'])
    db.execute("UPDATE tipos_empaque SET stock_actual=? WHERE id=?", (nuevo_stock, emp_id))
    db.commit()
    flash('Stock de empaque actualizado.', 'success')
    db.close()
    return redirect(url_for('inventario_empaques'))

@app.route('/inventario/empaques/nuevo', methods=['POST'])
def nuevo_empaque():
    db = get_db()
    nombre = request.form['nombre']
    producto_id = request.form.get('producto_id') or None
    stock_minimo = int(request.form.get('stock_minimo', 100))
    db.execute("INSERT INTO tipos_empaque (nombre, producto_id, stock_minimo) VALUES (?,?,?)",
               (nombre, producto_id, stock_minimo))
    db.commit()
    flash('Empaque agregado.', 'success')
    db.close()
    return redirect(url_for('inventario_empaques'))

# ─── Compras MP ────────────────────────────────────────────────────────────────

@app.route('/compras/mp', methods=['GET', 'POST'])
def compras_mp():
    db = get_db()
    if request.method == 'POST':
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
        accion = request.form.get('accion')
        if accion == 'nuevo':
            db.execute("INSERT INTO operarios (nombre, cedula, cargo) VALUES (?,?,?)",
                       (request.form['nombre'], request.form.get('cedula', ''), request.form.get('cargo', 'Operario')))
            flash('Operario agregado.', 'success')
        elif accion == 'toggle':
            op_id = request.form['operario_id']
            db.execute("UPDATE operarios SET activo = 1 - activo WHERE id=?", (op_id,))
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
        WHERE strftime('%Y-%m', lp.fecha_produccion)=?
        GROUP BY lp.producto_id
    """, (mes,)).fetchall()

    consumo_mp_mes = db.execute("""
        SELECT mp.nombre, SUM(lc.cantidad_usada) as total, lc.unidad
        FROM lote_consumo_mp lc
        JOIN materias_primas mp ON mp.id=lc.materia_prima_id
        JOIN lotes_produccion lp ON lp.id=lc.lote_id
        WHERE strftime('%Y-%m', lp.fecha_produccion)=?
        GROUP BY lc.materia_prima_id, lc.unidad
        ORDER BY total DESC
    """, (mes,)).fetchall()

    compras_mp_mes = db.execute("""
        SELECT mp.nombre, SUM(c.cantidad) as total, c.unidad,
               SUM(c.precio_total) as costo
        FROM compras_mp c JOIN materias_primas mp ON mp.id=c.materia_prima_id
        WHERE strftime('%Y-%m', c.fecha)=?
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
def eliminar_ingrediente(prod_id, ing_id):
    db = get_db()
    db.execute("DELETE FROM formula_ingredientes WHERE id=? AND producto_id=?", (ing_id, prod_id))
    db.commit()
    flash('Ingrediente eliminado.', 'info')
    db.close()
    return redirect(url_for('productos_lista'))

@app.route('/productos/<int:prod_id>/editar', methods=['POST'])
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
    db.commit()
    art = db.execute("SELECT nombre FROM articulos_bodega WHERE id=?", (art_id,)).fetchone()
    flash(f'Entrada registrada: +{cantidad} {art["nombre"]}.', 'success')
    db.close()
    return redirect(url_for('bodega'))

@app.route('/bodega/salida', methods=['POST'])
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
    db.commit()
    flash(f'Salida registrada: -{cantidad} {art["nombre"]}.', 'success')
    db.close()
    return redirect(url_for('bodega'))

@app.route('/bodega/ajuste', methods=['POST'])
def bodega_ajuste():
    db = get_db()
    art_id = int(request.form['articulo_id'])
    nuevo  = int(request.form['nuevo_stock'])
    db.execute("UPDATE articulos_bodega SET stock_actual=? WHERE id=?", (nuevo, art_id))
    db.commit()
    flash('Stock ajustado correctamente.', 'info')
    db.close()
    return redirect(url_for('bodega'))

# ─── Startup ──────────────────────────────────────────────────────────────────
# Inicializar DB al arrancar (necesario para Gunicorn en Railway)
try:
    init_db()
except Exception as e:
    print(f"DB init warning: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5050)
