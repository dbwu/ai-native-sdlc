"""从已确认的教学知识版本构造选课项目的逻辑架构与业务流程候选。"""

from .project_model import ProjectModel
from .semantics import KnowledgeError
from .service import KnowledgeService as KS


_RELEASES = {
    'v1': ('基础选课与幂等。', (), ()),
    'v1.1': ('增加余位检查、满员拒绝和最后一个名额的并发保护。', ('REQ-CAPACITY',), ('BR-CAPACITY',)),
    'v1.2': ('保留容量和幂等规则，增加同一学生的课表冲突检查。',
             ('REQ-CAPACITY', 'REQ-TIME-CONFLICT'), ('BR-CAPACITY', 'BR-TIME-CONFLICT')),
}


def _knowledge_refs(conn, project_id, release):
    rows = conn.execute("""SELECT b.external_key, b.entity_id AS id, m.version
        FROM knowledge_source s
        JOIN source_revision r ON r.source_id=s.id
        JOIN current_proposals p ON p.source_id=s.id AND p.source_version=r.version
        CROSS JOIN LATERAL jsonb_to_recordset(p.result->'entity_mapping')
            AS m(key text, entity_id uuid, version integer)
        JOIN source_entity_binding b ON b.source_id=s.id AND b.external_key=m.key AND b.entity_id=m.entity_id
        WHERE s.project_id=%s AND s.uri='urn:tutorial:course-selection:spec'
          AND r.external_revision=%s AND p.status='committed'""", (project_id, release)).fetchall()
    return {row['external_key']: {'id': row['id'], 'version': row['version']} for row in rows}


def _bound_node(conn, refs, key, source_key, kind, parent_key=None):
    ref = refs[source_key]
    entity = KS._get(conn, 'entity', ref['id'], ref['version'])
    return {'key': key, 'name': entity['name'], 'kind': kind, 'parent_key': parent_key,
            'description': entity['description'], 'entity_id': entity['id'], 'entity_version': ref['version']}


def _module(key, name, parent_key, description):
    return {'key': key, 'name': name, 'kind': 'module', 'parent_key': parent_key, 'description': description}


def _dependency(source, target, label):
    return {'source': source, 'target': target, 'label': label}


def _step(key, name, kind, owner):
    return {'key': key, 'name': name, 'kind': kind, 'owner': owner}


def _transition(source, target, condition=None):
    return {'source': source, 'target': target, 'condition': condition}


def _scenario(key, name, messages):
    return {'key': key, 'name': name,
            'messages': [{'key': identity, 'source': source, 'target': target, 'label': label}
                         for identity, source, target, label in messages]}


def _enroll_process(release, requirements, rules):
    steps = [
        _step('Submit', '提交选课', 'start', 'Student'),
        _step('CheckOffering', '教学班是否存在', 'decision', 'Offering'),
        _step('InvalidOffering', '返回教学班不存在', 'end', 'Student'),
        _step('CheckExisting', '是否已有选课记录', 'decision', 'Record'),
        _step('ReturnExisting', '返回原记录', 'end', 'Student'),
    ]
    transitions = [
        _transition('Submit', 'CheckOffering'),
        _transition('CheckOffering', 'InvalidOffering', '不存在'),
        _transition('CheckOffering', 'CheckExisting', '存在'),
        _transition('CheckExisting', 'ReturnExisting', '已有记录'),
    ]
    enter = ('CheckExisting', '首次选课')
    initial = [
        ('Request', 'Student', 'Enrollment', '提交选课请求'),
        ('ConfirmOffering', 'Enrollment', 'Offering', '确认教学班存在'),
        ('FindExisting', 'Enrollment', 'Record', '查找当前学生的原选课记录'),
    ]
    checks = []
    scenarios = [_scenario('Duplicate', '重复请求返回原记录', [
        *initial, ('ExistingRecord', 'Record', 'Enrollment', '返回原选课记录'),
        ('ReturnExisting', 'Enrollment', 'Student', '返回同一条记录'),
    ])]
    if release == 'v1.2':
        steps.extend([_step('CheckTimetable', '检查课表冲突', 'decision', 'Timetable'),
                      _step('RejectTimetable', '拒绝时间冲突的选课', 'end', 'Student')])
        transitions.extend([_transition(enter[0], 'CheckTimetable', enter[1]),
                            _transition('CheckTimetable', 'RejectTimetable', '课表重叠')])
        enter = ('CheckTimetable', '课表不重叠')
        checks.extend([('CheckTimetable', 'Enrollment', 'Timetable', '在事务内比较当前学生的课表'),
                       ('ReadScheduleRecords', 'Timetable', 'Record', '读取该学生的已选记录'),
                       ('ReadSchedules', 'Timetable', 'Offering', '读取目标及已选教学班的周课表')])
        scenarios.append(_scenario('TimeConflict', '课表冲突拒绝新增', [
            *initial, *checks, ('ConflictFound', 'Timetable', 'Enrollment', '发现固定周课表重叠'),
            ('ReturnConflict', 'Enrollment', 'Student', '返回时间冲突'),
        ]))
    if release in ('v1.1', 'v1.2'):
        steps.extend([_step('CheckCapacity', '检查余位', 'decision', 'Capacity'),
                      _step('RejectCapacity', '满员拒绝', 'end', 'Student')])
        transitions.extend([_transition(enter[0], 'CheckCapacity', enter[1]),
                            _transition('CheckCapacity', 'RejectCapacity', '无余位')])
        enter = ('CheckCapacity', '有余位')
        checks.extend([('CheckCapacity', 'Enrollment', 'Capacity', '在事务内检查剩余名额'),
                       ('ReadCapacity', 'Capacity', 'Offering', '读取教学班容量'),
                       ('CountRecords', 'Capacity', 'Record', '统计当前有效选课人数')])
        scenarios.append(_scenario('Full', '满员拒绝新增', [
            *initial, *checks, ('CapacityFull', 'Capacity', 'Enrollment', '确认没有余位'),
            ('ReturnFull', 'Enrollment', 'Student', '返回满员结果'),
        ]))
    steps.extend([_step('Persist', '写入选课记录', 'task', 'Record'), _step('Success', '返回选课成功', 'end', 'Student')])
    transitions.extend([_transition(enter[0], 'Persist', enter[1]), _transition('Persist', 'Success')])
    write_label = {'v1': '写入选课记录并提交事务', 'v1.1': '确认余位后写入选课记录并提交事务',
                   'v1.2': '校验课表和余位后写入选课记录并提交事务'}[release]
    scenarios.insert(0, _scenario('Success', '首次选课成功', [
        *initial, *checks, ('Persist', 'Enrollment', 'Record', write_label),
        ('ReturnNew', 'Enrollment', 'Student', '返回新选课记录'),
    ]))
    return {'key': 'SubmitEnrollment', 'name': '提交选课', 'description': '处理首次选课与重复请求，保持选课记录唯一。',
            'steps': steps, 'transitions': transitions, 'participants': [{'key': 'Student', 'name': '学生'}],
            'scenarios': scenarios, 'requirement_refs': requirements, 'rule_refs': rules}


def _view_process(requirement):
    return {'key': 'ViewEnrollments', 'name': '查看已选课程', 'description': '按当前学生查询，只返回其自己的选课记录。',
            'steps': [_step('Open', '打开已选列表', 'start', 'Student'),
                      _step('Query', '查询当前学生的记录', 'task', 'Record'),
                      _step('ReadNames', '获取教学班名称', 'task', 'Offering'),
                      _step('Show', '显示已选课程', 'end', 'Student')],
            'transitions': [_transition('Open', 'Query'), _transition('Query', 'ReadNames'), _transition('ReadNames', 'Show')],
            'participants': [{'key': 'Student', 'name': '学生'}],
            'scenarios': [_scenario('List', '查询本人已选课程', [
                ('Request', 'Student', 'Enrollment', '查询本人已选课程'),
                ('Filter', 'Enrollment', 'Record', '按当前学生筛选记录'),
                ('ReadNames', 'Record', 'Offering', '查询对应教学班名称'),
                ('Return', 'Enrollment', 'Student', '返回本人已选列表'),
            ])], 'requirement_refs': [requirement]}


def _browse_process(requirement):
    return {'key': 'BrowseOfferings', 'name': '浏览教学班目录', 'description': '列出当前学期教学班及课程名称和授课教师。',
            'steps': [_step('Open', '打开课程目录', 'start', 'Student'),
                      _step('List', '查询当前学期教学班', 'task', 'Catalog'),
                      _step('Read', '读取教学班基本信息', 'task', 'Offering'),
                      _step('Show', '显示教学班目录', 'end', 'Student')],
            'transitions': [_transition('Open', 'List'), _transition('List', 'Read'), _transition('Read', 'Show')],
            'participants': [{'key': 'Student', 'name': '学生'}],
            'scenarios': [_scenario('List', '查看本学期教学班', [
                ('Request', 'Student', 'Catalog', '查询教学班目录'),
                ('Read', 'Catalog', 'Offering', '读取课程和教学班基本信息'),
                ('OfferingInfo', 'Offering', 'Catalog', '返回教学班信息'),
                ('Return', 'Catalog', 'Student', '显示教学班列表'),
            ])], 'requirement_refs': [requirement]}


def course_model(conn, project_id, release):
    summary, requirements, rules = _RELEASES[release]
    refs = _knowledge_refs(conn, project_id, release)
    required = ('DOMAIN-COURSE', 'COMP-ENROLL', 'REQ-ENROLL', 'BR-UNIQUE', *requirements, *rules)
    if any(key not in refs for key in required):
        raise KnowledgeError(f'请先导入、校验并确认提交选课教学知识 {release}', 409)

    nodes = [
        _bound_node(conn, refs, 'CourseDomain', 'DOMAIN-COURSE', 'domain'),
        _bound_node(conn, refs, 'Enrollment', 'COMP-ENROLL', 'module', 'CourseDomain'),
        _module('Record', '选课记录', 'Enrollment', '保存学生与教学班的唯一选课关系，并查询本人记录。'),
        {'key': 'CatalogDomain', 'name': '开课目录领域', 'kind': 'domain', 'parent_key': None,
         'description': '提供学期教学班目录及其基础数据。'},
        _module('Catalog', '课程目录', 'CatalogDomain', '提供课程及教学班的浏览入口。'),
        _module('Offering', '教学班信息', 'Catalog', '提供教学班标识、名称、容量及固定周课表。'),
    ]
    dependencies = [
        _dependency('Catalog', 'Offering', '查询当前学期教学班'),
        _dependency('Enrollment', 'Offering', '确认教学班信息'),
        _dependency('Enrollment', 'Record', '查询并保存选课记录'),
        _dependency('Record', 'Offering', '取得已选教学班名称'),
    ]
    if release in ('v1.1', 'v1.2'):
        nodes.append(_module('Capacity', '余位校验', 'Enrollment', '在与新增写入相同的事务内判断零容量或满员。'))
        dependencies.extend([_dependency('Enrollment', 'Capacity', '校验剩余名额'),
                             _dependency('Capacity', 'Offering', '读取教学班容量'),
                             _dependency('Capacity', 'Record', '统计有效选课人数')])
    if release == 'v1.2':
        nodes.append(_module('Timetable', '课表冲突校验', 'Enrollment', '比较目标教学班与本人已选教学班的固定周课表。'))
        dependencies.extend([_dependency('Enrollment', 'Timetable', '校验课表冲突'),
                             _dependency('Timetable', 'Offering', '读取教学班课表'),
                             _dependency('Timetable', 'Record', '读取本人已选记录')])
    return ProjectModel.model_validate({
        'title': f'选课系统项目基线 {release}',
        'summary': summary + '领域与模块是教学用逻辑划分，沿用选课参考应用的业务行为。',
        'nodes': nodes, 'dependencies': dependencies,
        'processes': [_enroll_process(release, [refs[key] for key in ('REQ-ENROLL', *requirements)],
                                      [refs[key] for key in ('BR-UNIQUE', *rules)]),
                      _browse_process(refs['REQ-ENROLL']), _view_process(refs['REQ-ENROLL'])],
    })
