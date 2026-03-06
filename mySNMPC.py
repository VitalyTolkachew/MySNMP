#UTF-8
import os
import re
import subprocess
import time
import base64
import threading
import time

from queue import PriorityQueue, Empty
from icmplib import ping as icmp_ping
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask_cors import CORS
from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_basicauth import BasicAuth
from apscheduler.schedulers.background import BackgroundScheduler



runtime_states = {} #Для статусов  { 'n123': 'up', 'l456': 'down' }
# Очередь задач: (приоритет, id, адрес, имя, тип)
check_queue = PriorityQueue()
monitor_cache = {}  # Для времени проверок: {'n1': 1708851234.5}
# Список возможных форм 
CY_SHAPES = ['rectangle', 'round-rectangle', 'ellipse', 'triangle','diamond', 'pentagon', 'hexagon', 'octagon', 'star', 'barrel']
groups_cache = {}     # Параметры групп: {'23': {'icon': 'cisco.png', 'shape': 'diamond'}}
CRITICAL_GROUPS = []  # Список ID критических групп из БД: ['23', '1', 'Routers']

app = Flask(__name__)
CORS(app)

# настройка авторизации
app.config['BASIC_AUTH_USERNAME'] = 'admin'
app.config['BASIC_AUTH_PASSWORD'] = 'adminkspd'
app.config['BASIC_AUTH_FORCE'] = True
basic_auth = BasicAuth(app)

# Настройки БД и путей
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'network.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"connect_args": {"timeout": 30, "check_same_thread": False}}
app.config['BITMAPS_FOLDER'] = 'static/bitmaps'
app.config['JSON_AS_ASCII'] = False

db = SQLAlchemy(app)
# --- МОДЕЛИ ДАННЫХ (на основе SNMPc-Export) ---

class SubNet(db.Model):
    id = db.Column(db.Integer, primary_key=True) # это subnet_id
    label = db.Column(db.String(100))
    address = db.Column(db.String(50))
    parent_id = db.Column(db.Integer, index=True)

class NetworkObject(db.Model):
    id = db.Column(db.Integer, primary_key=True) # это node_id или goto_id
    obj_type = db.Column(db.String(10)) # 'host', 'goto', 'link'
    label = db.Column(db.String(100))
    address = db.Column(db.String(50)) # IP или DNS
    parent_id = db.Column(db.Integer, index=True) # ID подразделения
    icon_file = db.Column(db.String(100))
    group_name = db.Column(db.String(50))
    # Координаты для карты
    pos_x = db.Column(db.Float, default=100.0)
    pos_y = db.Column(db.Float, default=100.0)
    status = db.Column(db.String(10), default='up') # 'up' или 'down'
    ping_period = db.Column(db.Integer, default=60)
    description = db.Column(db.Text)          # Примечания
    get_community = db.Column(db.String(50))  # SNMP Community
    has_snmp = db.Column(db.Integer, default=0) # Тип мониторинга (0-ICMP, 1-SNMP)

class Link(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(100))
    address = db.Column(db.String(50))
    parent_id = db.Column(db.Integer, index=True)
    start_obj_id = db.Column(db.Integer) # ID хоста или перехода
    end_obj_id = db.Column(db.Integer)
    status = db.Column(db.String(10), default='up') 
    speed = db.Column(db.Integer, default=100)
    ping_period = db.Column(db.Integer, default=60)
    get_community = db.Column(db.String(50), default='public')
    has_snmp = db.Column(db.Integer, default=0)
    description = db.Column(db.Text)

class History(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    obj_id = db.Column(db.Integer)
    obj_label = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.now)
    event = db.Column(db.String(200))

class GroupDirectory(db.Model):
    group_id = db.Column(db.String(50), primary_key=True) # Код группы (напр. '23')
    label = db.Column(db.String(30))                     # Понятное имя
    icon_file = db.Column(db.String(100))                # Иконка по умолчанию
    shape = db.Column(db.String(20), default='rectangle') # Фигура на карте
    is_critical = db.Column(db.Boolean, default=False)   # Влияние на цвет



def sync_groups_to_memory():
    """Загрузка справочника в оперативную память"""
    global groups_cache, CRITICAL_GROUPS
    with app.app_context():
        # Предзаполнение, если база пуста
        if GroupDirectory.query.count() == 0:
            defaults = [('23', 'Маршрутизаторы', True, 'diamond', 'cisco.png'), 
                        ('1', 'Ядро', True, 'ellipse', 'asb.png'),
                        ('Routers', 'Routers', True, 'diamond', 'cisco.png')]
            for gid, lbl, crit, shp, icn in defaults:
                db.session.add(GroupDirectory(group_id=gid, label=lbl, is_critical=crit, shape=shp, icon_file=icn))
            db.session.commit()

        all_groups = GroupDirectory.query.all()
        groups_cache = {
            g.group_id: {"icon": g.icon_file, 
                         "shape": g.shape, 
                         "is_crit": g.is_critical} 
            for g in all_groups
        }
        CRITICAL_GROUPS = [g.group_id for g in all_groups if g.is_critical]
    print(f"--- Справочник синхронизирован. Групп: {len(groups_cache)}, Критичных: {len(CRITICAL_GROUPS)} ---")


def get_unit_status(unit_id):
    # ПРИОРИТЕТЫ: 3-Red, 2-Blue, 1-Green, 0-Gray, -1-Empty
    
    # Инициализируем переменную СРАЗУ
    current_worst = -1 
    
    try:
        objs = NetworkObject.query.filter_by(parent_id=unit_id).all()
        
        if objs:
            # Если есть объекты, начальный статус - серый (0) 
            # или зеленый (1), если есть хотя бы один активный
            has_active = any((getattr(o, 'ping_period', 60) or 0) > 0 for o in objs)
            current_worst = 1 if has_active else 0

            for n in objs:
                p_period = getattr(n, 'ping_period', 60) or 0
                if p_period == 0:
                    continue
                
                n_status = n.status or 'up'
                if n_status == 'down':
                    g_name = str(n.group_name or "").strip()
                    if g_name in CRITICAL_GROUPS:
                        current_worst = 3
                        break
                    else:
                        current_worst = max(current_worst, 2)
                else:
                    current_worst = max(current_worst, 1)

        # РЕКУРСИЯ (если текущий узел еще не красный)
        if current_worst < 3:
            child_subnets = SubNet.query.filter_by(parent_id=unit_id).all()
            for child in child_subnets:
                child_status = get_unit_status(child.id)
                current_worst = max(current_worst, child_status)
                if current_worst == 3: break
                
        return current_worst
    except Exception as e:
        print(f"Ошибка в unit {unit_id}: {e}")
        return -1

# --- ЛОГИКА МОНИТОРИНГА (ICMP) ---

def sync_runtime_states():
    """ Загрузка начальных статусов из БД в память при старте """
    with app.app_context():
        hosts = NetworkObject.query.all()
        links = Link.query.all()
        for h in hosts: runtime_states[f"n{h.id}"] = h.status or 'unknown'
        for l in links: runtime_states[f"l{l.id}"] = l.status or 'unknown'
        print(f"--- Память синхронизирована: {len(runtime_states)} объектов ---")

def get_uid(oid, is_host):
    return f"{'n' if is_host else 'l'}{oid}"

def do_reliable_ping(address):
    """ 3 пакета + 2 перепроверки через 5 сек """
    try:
        res = icmp_ping(address, count=3, interval=0.2, timeout=1)
        if res.is_alive: return True
        # Повторы при неудаче
        for _ in range(2):
            time.sleep(5)
            if icmp_ping(address, count=6, interval=0.2, timeout=1).is_alive:
                return True
        return False
    except: return False


def process_ping(obj_id, address, label, is_host):
    uid = get_uid(obj_id, is_host)
    
    # 1. Проверяем, не удален ли объект из памяти пока задача ждала в очереди
    if uid not in runtime_states: return

    # 2. Сетевая проверка
    alive = do_reliable_ping(address)
    new_status = 'up' if alive else 'down'

    # 3. Сравнение с памятью (без БД)
    old_status = runtime_states.get(uid, 'unknown')

    if old_status != new_status:
        runtime_states[uid] = new_status # Сразу обновляем память
        
        # 4. Запись в БД только при реальном изменении
        with app.app_context():
            try:
                model = NetworkObject if is_host else Link
                obj = db.session.get(model, obj_id)
                if obj:
                    obj.status = new_status 
                    msg = f"Статус изменился: {old_status} -> {new_status}"
                    db.session.add(History(obj_id=obj.id, obj_label=obj.label, event=msg))
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Ошибка записи в БД: {e}")
            finally:
                db.session.remove()


def scheduler_loop():
    """ Сканирует БД на предмет периодов и наполняет очередь раз в 2 сек """
    while True:
        with app.app_context():
            now = time.time()
            # Собираем всех, у кого задан мониторинг
            targets = NetworkObject.query.filter(NetworkObject.ping_period > 0).all() + \
                      Link.query.filter(Link.ping_period > 0).all()
            
            for item in targets:
                is_h = isinstance(item, NetworkObject)
                uid = get_uid(item.id, is_h)
                
                # Если объекта еще нет в кэше времени (monitor_cache из прошлых шагов)
                # или прошло время периода — в очередь!
                # (monitor_cache хранит только timestamp последней проверки)
                state = monitor_cache.get(uid, 0)
                if now - state >= item.ping_period:
                    check_queue.put((1, item.id, item.address, item.label, is_h))
                    monitor_cache[uid] = now
        time.sleep(2) # таймер сканирования БД


def executor_loop():
    while True:
        try:
            _, oid, addr, lbl, is_h = check_queue.get(timeout=1)
            process_ping(oid, addr, lbl, is_h)
            check_queue.task_done()
        except Empty: continue


def force_check(obj_id, is_host=True):
    """Принудительный вброс в начало очереди (Приоритет 0)"""
    with app.app_context():
        model = NetworkObject if is_host else Link
        obj = db.session.get(model, obj_id)
        if obj and obj.address and obj.address != '0.0.0.0':
            # 0 - самый высокий приоритет в PriorityQueue
            check_queue.put((0, obj.id, obj.address, obj.label, is_host))

#-----------------------------------------------------------------
def ping_worker(obj_id, ip, target_type): # Добавили target_type
    """ Функция проверки одного хоста или линка """
    def do_ping(count):
        param = '-n' if os.name == 'nt' else '-c'
        # Добавлен таймаут и подавление вывода
        res = subprocess.run(['ping', param, str(count), '-w', '1000', ip], 
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0

    if not ip: return

    is_alive = do_ping(1)
    if not is_alive:
        time.sleep(0.2)
        is_alive = do_ping(2)

    new_status = 'up' if is_alive else 'down'

    with app.app_context():
        # Используем target_type вместо ошибочного type
        if target_type == "host":
            obj = db.session.get(NetworkObject, obj_id)
        else:
            if target_type == "link": 
                obj = db.session.get(Link, obj_id)
            else:
                return
        if obj and obj.status != new_status:
            print(f"[MONITOR] {target_type} {obj.label} changed: {obj.status} -> {new_status}")
            obj.status = new_status
            # msg = f"Статус {'линка' if target_type=='link' else 'объекта'} {obj.label} -> {new_status}"
            msg = f"-> {new_status}"
            db.session.add(History(obj_id=obj.id, obj_label=obj.label, event=msg))
            db.session.commit()

def run_monitoring_cycle():
    print(f"--- Цикл мониторинга начат: {datetime.now()} ---")
    with app.app_context():
        # Собираем хосты
        targets = NetworkObject.query.filter(
           NetworkObject.address != None, 
           NetworkObject.address != '',
           NetworkObject.address != '0.0.0.0',
           NetworkObject.obj_type != 'goto').all()
 
        # Собираем линки
        link_targets = Link.query.filter(Link.address != None, Link.address != '', NetworkObject.address != '0.0.0.0').all()
        
        with ThreadPoolExecutor(max_workers=50) as executor:
            for t in targets:
                executor.submit(ping_worker, t.id, t.address, "host")

            for l in link_targets:
                executor.submit(ping_worker, l.id, l.address, "link")



# --- API ЭНДПОИНТЫ ---

@app.route('/api/get_shapes')
def get_shapes():
    return jsonify(CY_SHAPES)

@app.route('/api/groups', methods=['GET'])
def get_groups():
    groups = GroupDirectory.query.order_by(GroupDirectory.label).all()
    return jsonify([{"id": g.group_id, "label": g.label, "icon": g.icon_file, "shape": g.shape, "is_critical": g.is_critical} for g in groups])

@app.route('/api/groups/save', methods=['POST'])
def save_group():
    data = request.json
    g = GroupDirectory.query.get(data['id'])
    if not g:
        g = GroupDirectory(group_id=data['id'])
        db.session.add(g)
    g.label = data.get('label')
    g.icon_file = data.get('icon')
    g.shape = data.get('shape')
    g.is_critical = bool(data.get('is_critical'))
    db.session.commit()
    sync_groups_to_memory() # Обновляем кеш
    return jsonify({"status": "ok"})

@app.route('/api/groups/delete/<string:gid>', methods=['DELETE'])
def delete_group(gid):
    GroupDirectory.query.filter_by(group_id=gid).delete()
    db.session.commit()
    sync_groups_to_memory()
    

    return jsonify({"status": "ok"})

# Проверка пингом
@app.route('/api/ping_check')
def ping_check():
    addr = request.args.get('addr')
    param = '-n' if os.name == 'nt' else '-c'
    try:
        # Запускаем пинг (1 пакет) и забираем текст "как есть"
        res = subprocess.run(['ping', param, '1', '-w', '1000', addr], 
                             capture_output=True, text=True, encoding='cp866')
        
        # Очищаем вывод от пустых строк и заголовков (оставляем только суть)
        lines = res.stdout.splitlines()
        # Ищем строку, в которой есть ответ или ошибка (обычно это 2-я или 3-я строка)
        result_line = ""
        for line in lines:
            if "Ответ от" in line or "Превышен" in line or "Недоступен" in line or "Request timed out" in line:
                result_line = line
                break
        
        return jsonify({"output": result_line or "Нет ответа от системы"})
    except Exception as e:
        return jsonify({"output": f"Ошибка вызова: {str(e)}"})

# Просмотр истории
@app.route('/api/object_history/<int:obj_id>')
def object_history(obj_id):
    # Получаем логи и данные самого объекта одним запросом
    query = db.session.query(
        History, 
        NetworkObject.group_name, 
        NetworkObject.ping_period
    ).outerjoin(NetworkObject, History.obj_id == NetworkObject.id)\
     .filter(History.obj_id == obj_id)\
     .order_by(History.timestamp.desc())\
     .limit(50).all()
    
    output = []
    for h, g_name, period in query:
        st = 'up' if '-> up' in h.event else ('down' if '-> down' in h.event else 'unknown')
        is_crit = str(g_name or "").strip() in CRITICAL_GROUPS
        
        output.append({
            "time": h.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "event": h.event,
            "status": st,
            "is_critical": is_crit,
            "ping_period": period if period is not None else 60
        })
    return jsonify(output)

#Добавление подразделения в дерево
@app.route('/api/unit/add', methods=['POST'])
def add_unit():
    data = request.json
    # Если parent_id равен "#" (корень), ставим 0
    p_id = 0 if data['parent_id'] == "#" else int(data['parent_id'])
    new_unit = SubNet(label=data['label'], parent_id=p_id)
    db.session.add(new_unit)
    db.session.commit()
    return jsonify({"status": "ok", "id": new_unit.id})

# Переименование подразделения
@app.route('/api/unit/rename', methods=['POST'])
def rename_unit():
    data = request.json
    unit = SubNet.query.get(int(data['id']))
    if unit:
        unit.label = data['label']
        db.session.commit()
        return jsonify({"status": "ok"})
    return jsonify({"status": "error"}), 404

def delete_unit_recursive(unit_id):
    # 1. Находим все дочерние подразделения
    children = SubNet.query.filter_by(parent_id=unit_id).all()
    for child in children:
        delete_unit_recursive(child.id) # РЕКУРСИЯ
    
    # 2. Удаляем все объекты и связи в ЭТОМ подразделении
    NetworkObject.query.filter_by(parent_id=unit_id).delete()
    Link.query.filter_by(parent_id=unit_id).delete()
    
    # 3. Удаляем саму запись подразделения
    SubNet.query.filter_by(id=unit_id).delete()

# Удаление подразделения
@app.route('/api/unit/delete/<int:unit_id>', methods=['DELETE'])
def delete_unit_endpoint(unit_id):
    try:
        delete_unit_recursive(unit_id)
        db.session.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500



# Список иконок
@app.route('/api/get_bitmaps')
def get_bitmaps():
    bitmaps_path = os.path.join(app.static_folder, 'bitmaps')
    try:
        # Получаем список файлов с расширениями изображений
        files = [f for f in os.listdir(bitmaps_path) 
                 if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp'))]
        return jsonify(sorted(files))
    except Exception as e:
        return jsonify([]), 500

@app.route('/')
def index():
    return render_template('index.html')

# Отображение дерева подразделений
@app.route('/api/tree')
def get_tree():
    # 1. Всего два запроса к БД (на всё дерево)
    subnets = SubNet.query.all()
    all_objects = NetworkObject.query.all()
    
    # 2. Индексируем объекты по parent_id для мгновенного доступа
    objs_by_parent = {}
    for obj in all_objects:
        p_id = obj.parent_id
        if p_id not in objs_by_parent: objs_by_parent[p_id] = []
        objs_by_parent[p_id].append(obj)

    # 3. Индексируем структуру подразделений (кто чей ребенок)
    children_map = {}
    for s in subnets:
        if s.parent_id not in children_map: children_map[s.parent_id] = []
        children_map[s.parent_id].append(s)

    # Кеш статусов, чтобы не считать одно и то же дважды
    memo_status = {}

    def calc_status(unit_id):
        if unit_id in memo_status: return memo_status[unit_id]
        
        # Приоритеты: 3-Red, 2-Blue, 1-Green, 0-Gray, -1-Empty
        worst = -1
        
        # Проверяем объекты текущего подразделения (в памяти)
        unit_objs = objs_by_parent.get(unit_id, [])
        if unit_objs:
            #has_active = any((getattr(o, 'ping_period', 60) or 0) > 0 for o in unit_objs)
            has_active = any((getattr(o,'ping_period',0) or 0) > 0 for o in unit_objs)
            worst = 1 if has_active else 0
            for o in unit_objs:
                p_period = getattr(o, 'ping_period',0) or 0
                if p_period == 0: continue
                if o.status == 'down':
                   # worst = max(worst, 3 if (o.group_name or "").strip() in CRITICAL_GROUPS else 2)
                    is_crit = (o.group_name or "").strip() in CRITICAL_GROUPS
                    node_status = 3 if is_crit else 2
                    worst = max(worst, node_status)
                #else:
                #    worst = max(worst, 1)
                if worst == 3: break # Дальше считать нет смысла
        # Проверяем дочерние подразделения (рекурсия по индексу в памяти)
        for child in children_map.get(unit_id, []):
            #worst = max(worst, calc_status(child.id))
            child_status = calc_status(child.id)
            worst = max(worst, child_status)
            if worst == 3: break
        memo_status[unit_id] = worst
        return worst

    # 4. Формируем результат
    tree_data = []
    colors = {3: "red", 2: "blue", 1: "green", 0: "gray", -1: "black"}
    
    for s in subnets:
        code = calc_status(s.id)
        tree_data.append({
            "id": str(s.id),
            "parent": str(s.parent_id) if s.parent_id and s.parent_id != 0 else "#",
            "text":s.label, 
            "li_attr": {
                "style": f"color: {colors[code]}",
                "class": "unit-node"  # Добавим класс для удобства стилизации
            },
            "original": {"address": s.address}
        })
    return jsonify(tree_data)

# Отрисовка карты
@app.route('/api/map/<int:unit_id>')
def get_map(unit_id):
    try:
        nodes = NetworkObject.query.filter_by(parent_id=unit_id).all()
        links = Link.query.filter_by(parent_id=unit_id).all()
        # Собираем ID узлов для проверки связей
        node_ids = {f"n{n.id}" for n in nodes}
        elements = []

        # --- ОБРАБОТКА УЗЛОВ ---
        for n in nodes:
            # Берем статус ИЗ ПАМЯТИ. Если нет — из БД.
            m_status = runtime_states.get(f"n{n.id}", n.status or 'unknown')
            g_name = str(n.group_name or "").strip()
            is_critical = g_name in CRITICAL_GROUPS
            # 2. Берем настройки из справочника групп (из памяти groups_cache)
            g_info = groups_cache.get(g_name, {})
            is_crit = g_info.get('is_crit', False)
    
            # 3. ЛОГИКА ФОРМЫ (Shape): Берем из группы, если нет - дефолт 'rectangle'
            shape = g_info.get('shape', 'rectangle')
            # 4. ЛОГИКА иконки (Shape): Берем из группы, если нет - дефолт 'rectangle'
            if not n.icon_file:
               icon_file=g_info.get('icon') 
            else:
               icon_file=n.icon_file
            if not icon_file:
               icon_file = "workst.png"


            # Цвет считаем на основе статуса из памяти
            if m_status == 'up':
                color = "#28a745" # Зеленый
            elif m_status == 'down':
                color = "#dc3545" if is_critical else "#007bff" # Красный или Синий
            else:
                color = "#adb5bd" # Серый (unknown)

            if m_status == 'unknown': color = '#adb5bd' 
            # 4. Формирование пути к иконке
            icon_path = f"/static/bitmaps/{icon_file}" if icon_file else ""
            elements.append({
                "data": {
                    "id": f"n{n.id}",
                    "label": n.label,
                    "type": n.obj_type,
                    "group": g_name,
                    "address": n.address,
                    "is_critical": is_critical, 
                    "color": color,
                    "shape": shape, 
                    "icon": icon_path,
                    "ping_period": n.ping_period if n.ping_period is not None else 60,
                    "community": n.get_community, 
                    "has_snmp": n.has_snmp,       
                    "description": n.description, 
                    "status": n.status
                },
                "position": {
                   "x": float(n.pos_x) if n.pos_x is not None else 100.0,
                   "y": float(n.pos_y) if n.pos_y is not None else 100.0
                }
            })

        # --- ОБРАБОТКА СВЯЗЕЙ ---
        for l in links:
            source = f"n{l.start_obj_id}"
            target = f"n{l.end_obj_id}"
            # Добавляем линк только если оба узла существуют в этом подразделении


            if source in node_ids and target in node_ids:
                elements.append({
                    "data": {
                        "id": f"l{l.id}",
                        "source": f"n{l.start_obj_id}",
                        "target": f"n{l.end_obj_id}",
                        "label": l.label,
                        "address": l.address,
                        "status": l.status,
                        "speed": l.speed,
                        "ping_period": l.ping_period,
                        "community": l.get_community,
                        "has_snmp": l.has_snmp,
                        "description": l.description
                    }
                })

        return jsonify(elements)

    except Exception as e:
        # Если что-то пойдет не так, мы увидим текст ошибки в консоли
        print(f"Ошибка в get_map: {e}")
        return jsonify({"error": str(e)}), 500




@app.route('/api/history')
def get_history():
    # Соединяем историю с объектами, чтобы получить их параметры (группу и период)
    query = db.session.query(
        History, 
        NetworkObject.group_name, 
        NetworkObject.ping_period
    ).outerjoin(NetworkObject, History.obj_id == NetworkObject.id)\
     .order_by(History.timestamp.desc())\
     .limit(100).all()
    
    output = []
    for h, g_name, period in query:
        # Определяем статус события из текста лога
        status = 'up' if '-> up' in h.event else ('down' if '-> down' in h.event else 'unknown')
        # Проверяем критичность группы по вашему списку
        is_crit = str(g_name or "").strip() in CRITICAL_GROUPS
        
        output.append({
          "obj_id": h.obj_id,
            "time": h.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "time_full": h.timestamp.isoformat(),
            "obj_name": h.obj_label,
            "msg": h.event,
            "status": status,
            "is_critical": is_crit,
            "ping_period": period if period is not None else 0
        })
    return jsonify(output)

@app.route('/api/save_pos', methods=['POST'])
def save_pos():
    data = request.json
    # id приходит в формате "n123", убираем "n"
    clean_id = data['id'].replace('n', '')
    obj = db.session.get(NetworkObject, int(clean_id))
    if obj:
        obj.pos_x, obj.pos_y = data['x'], data['y']
        db.session.commit()
    return jsonify({"status": "ok"})

@app.route('/api/get_all_subnets')
def get_all_subnets():
    subs = SubNet.query.order_by(SubNet.label).all()
    # Возвращаем только уникальные названия, чтобы не путаться
    labels = sorted(list(set([s.label for s in subs])))
    return jsonify(labels)

@app.route('/api/add_link', methods=['POST'])
def add_link():
    data = request.json
    try:
        # Очищаем ID от префиксов 'n' на всякий случай
        s_id = int(str(data['start_id']).replace('n', ''))
        e_id = int(str(data['end_id']).replace('n', ''))
        
        new_link = Link(
            label=data.get('label') or "",
            address=data.get('address'),
            parent_id=int(data['parent_id']),
            start_obj_id=s_id,
            end_obj_id=e_id,
            speed=int(data.get('speed', 100)),
            ping_period=int(data.get('ping_period', 0)),
            get_community=data.get('community'), # в JSON шлем 'community'
            has_snmp=int(data.get('has_snmp', 0)),
            description=data.get('description'),
            status='unknown'
        )
        db.session.add(new_link)
        db.session.commit()
        # --- ОБНОВЛЕНИЕ МОНИТОРИНГА ---
        uid = f"l{new_link.id}"
        runtime_states[uid] = 'unknown'
        if new_link.address and new_link.address != '0.0.0.0':
            check_queue.put((0, new_link.id, new_link.address, new_link.label, False))
        return jsonify({"status": "ok", "id": new_link.id})
    except Exception as e:
        db.session.rollback()
        print(f"Ошибка при создании линка: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500



@app.route('/api/update_link', methods=['POST'])
def update_link():
    data = request.json
    try:
        link = db.session.get(Link, int(data['id']))
        if not link:
            return jsonify({"status": "error", "message": "Линк не найден"}), 404
        
        # Запоминаем, изменился ли адрес
        old_address = link.address
        
        link.label = data.get('label')
        link.address = data.get('address')
        link.speed = int(data.get('speed', 100))
        link.ping_period = int(data.get('ping_period', 0))
        link.get_community = data.get('community')
        link.has_snmp = int(data.get('has_snmp', 0))
        link.description = data.get('description')
        
        db.session.commit()

        # --- ОБНОВЛЕНИЕ МОНИТОРИНГА ---
        uid = f"l{link.id}"
        
        # Если адрес изменился или период стал > 0, инициируем проверку
        if link.address and (link.address != old_address or link.ping_period > 0):
            runtime_states[uid] = 'unknown' # Сбрасываем статус в памяти
            check_queue.put((0, link.id, link.address, link.label, False))
        
        # Если мониторинг отключили (период 0), убираем из активных состояний
        if link.ping_period == 0:
            runtime_states[uid] = 'unknown'
        return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/delete_link/<int:link_id>', methods=['DELETE'])
def delete_link(link_id):
    try:
        link = Link.query.get(link_id)
        if not link:
            return jsonify({"status": "error", "message": "Линк не найден"}), 404
        
        # Добавляем запись в историю перед удалением
        hist = History(obj_id=link.id, obj_label=link.label, event=f"Связь '{link.label}' удалена")
        db.session.add(hist)
        
        db.session.delete(link)
        db.session.commit()
        # 1. Удаляем из памяти МГНОВЕННО
        uid = f"l{link_id}"
        if uid in runtime_states: del runtime_states[uid]
        return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/add_object', methods=['POST'])
def add_object():
    data = request.json
    try:
        new_obj = NetworkObject(
            label=data.get('label'),
            address=data.get('address'),
            obj_type=data.get('type'),
            parent_id=int(data.get('parent_id')),
            group_name=data.get('group'),
            ping_period=int(data.get('ping_period', 60)),
            icon_file=data.get('icon'), 
            has_snmp=int(data.get('has_snmp', 0)),
            get_community=data.get('community', 'public'),
            description=data.get('description', ''),
            pos_x=150.0, 
            pos_y=150.0, 
            status='unknown'
        )
        db.session.add(new_obj)
        db.session.flush() # Получаем ID до коммита для истории
        # Запись в историю
        msg = f"Создан новый объект: {new_obj.label} ({new_obj.obj_type})"
        hist = History(obj_id=new_obj.id, obj_label=new_obj.label, event=msg)
        db.session.add(hist)
        db.session.commit()
        uid = f"n{new_obj.id}"
        runtime_states[uid] = 'unknown' # Регистрируем в памяти
        # Если адрес есть, проверяем немедленно (priority 0)
        if new_obj.address and new_obj.address != '0.0.0.0':
            check_queue.put((0, new_obj.id, new_obj.address, new_obj.label, True))

        return jsonify({"status": "ok", "id": new_obj.id})
    except Exception as e:
        db.session.rollback()
        print(f"Ошибка при добавлении объекта: {e}")
        return jsonify({"status": "error"}), 500

# Редактирование свойств узлов сети
@app.route('/api/update_object', methods=['POST'])
def update_object():
    global cached_tree # Добавляем доступ к глобальной переменной кэша
    data = request.json
    try:
        

        obj = db.session.get(NetworkObject, int(data['id']))

        if obj:
            # Запоминаем старый адрес для проверки изменений
            old_addr = obj.address
            old_period = obj.ping_period
            obj.label = data.get('label')
            obj.address = data.get('address')
            obj.obj_type = data.get('type')
            obj.group_name = data.get('group')
            obj.icon_file = data.get('icon')
            obj.ping_period = int(data.get('ping_period') )
            obj.has_snmp = int(data.get('has_snmp'))
            obj.description = data.get('description')
            obj.get_community = data.get('community')
            
            db.session.commit()
            uid = f"n{obj.id}"
            if obj.address != old_addr or (old_period == 0 and obj.ping_period > 0):
                runtime_states[uid] = 'unknown' # Регистрируем в памяти
            if obj.ping_period == 0:
                runtime_states[uid] = 'unknown'
            if obj.address and obj.address != '0.0.0.0':
                check_queue.put((0, obj.id, obj.address, obj.label, True))
            # КРИТИЧЕСКИ ВАЖНО: Сбрасываем кэш, чтобы дерево пересчиталось
            cached_tree = None 
            print(f"Объект {obj.id} успешно обновлен в БД. Кэш сброшен.")
            return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/delete_object/<int:obj_id>', methods=['DELETE'])
def delete_object(obj_id):
    try:
        # 1. Находим объект
        obj = NetworkObject.query.get(obj_id)
        if not obj:
            return jsonify({"status": "error", "message": "Объект не найден"}), 404

        # 2. Удаляем все линки, где этот объект является началом или концом
        # start_obj_id и end_obj_id в таблице Link
        Link.query.filter((Link.start_obj_id == obj_id) | (Link.end_obj_id == obj_id)).delete()

        # 3. Добавляем запись в историю об удалении
        hist = History(
            obj_id=obj_id, 
            obj_label=obj.label, 
            event=f"Объект '{obj.label}' удален пользователем вместе со всеми связями"
        )
        db.session.add(hist)

        # 4. Удаляем сам объект
        db.session.delete(obj)
        db.session.commit()
        uid = f"n{obj_id}"
        if uid in runtime_states:
            del runtime_states[uid]

        return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/find_object_by_name')
def find_object_by_name():
    # Пробуем получить имя из Base64 параметра
    b64name = request.args.get('b64name')
    
    if b64name:
        try:
            # Декодируем обратно в строку UTF-8
            name = base64.b64decode(b64name).decode('utf-8')
        except Exception as e:
            return jsonify({"status": "error", "message": "Invalid encoding"}), 400
    else:
        # Резервный вариант (обычный текст)
        name = request.args.get('name')

    print(f"Ищу в базе точное совпадение: '{name}'")

    # Ищем объект. Используем filter для точности в SQLite
    obj = NetworkObject.query.filter(NetworkObject.label == name).first()
    if obj:
        return jsonify({
            "status": "success", 
            "parent_id": str(obj.parent_id), 
            "obj_id": f"n{obj.id}"
        })
    
    # Проверка в линках
    link = Link.query.filter(Link.label == name).first()
    

    if link:
        return jsonify({
            "status": "success", 
            "parent_id": str(link.parent_id), 
            "obj_id": f"l{link.id}"
        })
    return jsonify({"status": "error", "message": "Not found"}), 404

@app.route('/api/global_search')
def global_search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"status": "error", "message": "Пустой запрос"}), 400

    # Проверяем наличие масок
    has_wildcard = '*' in query or '?' in query
    
    if has_wildcard:
        # Преобразуем маски для SQL (SQLite/PostgreSQL)
        sql_pattern = query.replace('*', '%').replace('?', '_')
        
        # Поиск по узлам (LIKE)
        obj = NetworkObject.query.filter(
            (NetworkObject.label.like(sql_pattern)) | 
            (NetworkObject.address.like(sql_pattern))
        ).first()
        
        # Поиск по линкам (LIKE), если узел не найден
        if not obj:
            obj = Link.query.filter(
                (Link.label.like(sql_pattern)) | 
                (Link.address.like(sql_pattern))
            ).first()
    else:
        # ЖЕСТКИЙ ПОИСК (Точное совпадение =)
        obj = NetworkObject.query.filter(
            (NetworkObject.label == query) | 
            (NetworkObject.address == query)
        ).first()
        
        if not obj:
            obj = Link.query.filter(
                (Link.label == query) | 
                (Link.address == query)
            ).first()

    if obj:
        # Определяем префикс n для узла/перехода, l для линка
        prefix = 'n' if isinstance(obj, NetworkObject) else 'l'
        return jsonify({
            "status": "success",
            "parent_id": str(obj.parent_id),
            "obj_id": f"{prefix}{obj.id}",
            "label": obj.label
        })

    return jsonify({"status": "error", "message": "Ничего не найдено"}), 404


# --- ЗАПУСК ---

if __name__ == '__main__':
    if not os.path.exists(os.path.join(basedir, 'network.db')):
        with app.app_context():
            db.create_all()
            print("База данных создана.")
    
    sync_groups_to_memory()  # CRITICAL_GROUPS заполнен из БД
    sync_runtime_states() # 1. Синхронизируем память

    # Запуск планировщика
    threading.Thread(target=scheduler_loop, daemon=True).start()
    
    # Запуск 40 параллельных исполнителей (для "пакетной" обработки)
    for _ in range(40):
        threading.Thread(target=executor_loop, daemon=True).start()

    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
