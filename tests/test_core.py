from __future__ import annotations

import copy
import pytest
from contextlib import contextmanager
from vsql.models import QueryPlan, Table, Column, Projection, Relation, Filter, table_key
from vsql.database import DatabaseGateway, ConnectionConfig, get_error_detail, _odbc_value, MAX_METADATA_COLUMNS
from vsql.security import check_generated_sql, validate_compiled_plan, check_sql_ast_if_available
from vsql.sql_builder import SQLBuilder, quote_identifier
from vsql.storage import save_workspace, load_workspace
from vsql.local_ai import apply_ai_patch, table_ids, metadata_prompt, OllamaLocalAI


@pytest.fixture
def schema():
    # Synthetic *metadata* in unit tests only. There is no runtime demonstration database.
    a = Table('test', 'A', [Column('ref', 'TEXT'), Column('memo', 'TEXT')])
    b = Table('test', 'B', [Column('id', 'TEXT'), Column('name', 'TEXT'), Column('gender', 'CHAR(1)'), Column('address','TEXT')])
    return {a.key: a, b.key: b}


@pytest.fixture
def plan(schema):
    a, b = list(schema)
    p=QueryPlan(name='Test', dialect='mysql')
    p.add_node(schema[a])
    p.add_node(schema[b])
    p.base=b
    p.projections=[Projection(b,'name'),Projection(b,'address')]
    p.joins=[Relation(a,'ref',b,'id','EXISTS')]
    p.filters=[Filter(b,'gender','=','M')]
    return p


def test_startup_disconnected_without_demo():
    db=DatabaseGateway()
    assert not db.connected
    assert db.tables=={}
    assert db.description=='Not connected'
    db.close()


def test_connection_config_requires_valid_five_fields():
    for item in (ConnectionConfig(), ConnectionConfig(user='reader',database='x',port=0),
                 ConnectionConfig(kind='sqlite',user='reader',database='x')):
        with pytest.raises(ValueError): item.validate()
    c=ConnectionConfig(kind='mysql',host='localhost',port=3306,user='read',database='prod')
    c.validate()


class FakeCursor:
    def __init__(self,conn):
        self.conn=conn
        self.timeout=None
        self.read_count=0
    def execute(self,sql,params=()):
        self.conn.commands.append((sql,params))
    def fetchone(self):
        return (1,)
    def fetchmany(self,n):
        if not any('information_schema' in sql.lower() for sql,_ in self.conn.commands):
            return []
        self.read_count += 1
        if self.read_count > 1:
            return []
        return [('test','B','name','TEXT','YES',''),('test','B','gender','CHAR(1)','YES','')]
    def close(self): pass

class FakeConnection:
    def __init__(self):
        self.commands=[]
        self.was_closed=False
        self.rolled_back=False
    def cursor(self):return FakeCursor(self)
    def rollback(self):self.rolled_back=True
    def close(self):self.was_closed=True


def test_connection_test_is_only_select_one_and_does_not_activate(monkeypatch):
    db=DatabaseGateway()
    fake=FakeConnection()
    @contextmanager
    def fake_connect(config):
        yield fake
    monkeypatch.setattr(db,'_connect',fake_connect)
    cfg=ConnectionConfig(user='readonly',database='test')
    assert db.test_connection(cfg) is True
    assert fake.commands==[('SELECT 1',())]
    assert db.tables=={} and not db.connected


def test_connect_reads_only_metadata_and_commits_active_settings(monkeypatch):
    db=DatabaseGateway()
    fake=FakeConnection()
    @contextmanager
    def fake_connect(config):
        yield fake
    monkeypatch.setattr(db,'_connect',fake_connect)
    cfg=ConnectionConfig(user='readonly',database='test')
    tables=db.open(cfg)
    assert db.connected and db.config==cfg
    assert len(tables)==1
    assert [x.name for x in next(iter(tables.values())).columns]==['name','gender']
    assert all('information_schema' in sql.lower() for sql,_ in fake.commands)


def test_compiler_uses_exists_and_gender_filter(plan,schema):
    sql,report=validate_compiled_plan(plan,schema)
    assert report.safe and 'EXISTS' in sql and 'gender' in sql
    assert 'SELECT' in sql and 'FROM' in sql


def test_projection_edit_keeps_join_and_filter(plan,schema):
    prior_join,prior_filter=copy.deepcopy(plan.joins),copy.deepcopy(plan.filters)
    plan.projections=[Projection(plan.base,'address')]
    sql=SQLBuilder(plan,schema).build().sql
    assert 'address' in sql and 'gender' in sql and 'EXISTS' in sql
    assert plan.joins==prior_join and plan.filters==prior_filter


def test_parameterized_filters_never_inline_untrusted_values(plan,schema):
    plan.filters=[Filter(plan.base,'name','=','x\'; DELETE FROM B; --')]
    built=SQLBuilder(plan,schema).build(parameterized=True)
    assert 'DELETE' not in built.sql
    assert built.params==("x'; DELETE FROM B; --",)


def test_sql_script_must_be_wholly_read_only():
    for sql in ('SELECT * FROM A; DELETE FROM B;', 'SELECT * INTO tmp FROM A;',
                'UPDATE B SET a=1;', 'SELECT * FROM A; CALL something();'):
        assert not check_generated_sql(sql).safe


def test_identifier_quoting_three_engines():
    assert quote_identifier('we`ird','mysql')=='`we``ird`'
    assert quote_identifier('we]ird','tsql')=='[we]]ird]'
    assert quote_identifier('we"ird','postgres')=='"we""ird"'


def test_all_supported_engines_compile_select(plan,schema):
    for dialect in ('mysql','postgres','tsql'):
        plan.dialect=dialect
        sql=SQLBuilder(plan,schema).build(limit=100).sql
        assert check_generated_sql(sql).safe
        assert ('TOP (100)' if dialect=='tsql' else 'LIMIT 100') in sql


def test_workspace_context_isolation(plan,tmp_path):
    plan.user_context=['old context']
    fresh=QueryPlan(dialect='mysql')
    assert not fresh.joins and not fresh.filters and not fresh.user_context
    name=tmp_path/'saved.vsql'
    save_workspace(name,plan)
    loaded=load_workspace(name)
    assert loaded.filters==plan.filters and not loaded.user_context
    assert 'old context' not in name.read_text(encoding='utf-8')


def test_ai_patch_changes_output_only(plan,schema):
    _,rev=table_ids(schema)
    p=apply_ai_patch(plan, {'action':'columns_only','columns':[{'table':rev[plan.base],'column':'name'}]},schema)
    assert [x.column for x in p.projections]==['name']
    assert p.joins==plan.joins and p.filters==plan.filters


def test_ai_rejects_hallucinated_table_or_column(plan,schema):
    with pytest.raises(ValueError):
        apply_ai_patch(plan,{'action':'columns_only','columns':[{'table':'T999','column':'x'}]},schema)
    _,rev=table_ids(schema)
    with pytest.raises(ValueError):
        apply_ai_patch(plan,{'action':'columns_only','columns':[{'table':rev[plan.base],'column':'unknown'}]},schema)


def test_local_ai_prompt_has_no_database_rows_or_password(plan,schema):
    data=metadata_prompt(schema,plan,'Show name and address')
    assert 'schema' in data and 'password' not in data.lower()
    assert 'SELECT *' not in data


def test_live_sql_ast_validator_fails_closed_if_dependency_missing():
    x=check_sql_ast_if_available('SELECT * FROM t;', 'mysql',required=True)
    assert x.safe or 'sqlglot' in x.message.lower()


def test_local_ai_cloud_models_forbidden():
    assert OllamaLocalAI._is_cloud('big-model:cloud')
    assert not OllamaLocalAI._is_cloud('qwen2.5-coder:1.5b')


def test_tls_modes_are_explicit_and_validate():
    base = dict(kind='mysql', host='192.168.1.10', user='reader', database='loans', port=3306)
    for mode in ('verify','trust','legacy'):
        c=ConnectionConfig(**base,tls_mode=mode)
        c.validate()
    with pytest.raises(ValueError):
        ConnectionConfig(**base,tls_mode='auto-insecure').validate()
    assert ConnectionConfig(**base).tls_mode == 'verify'


def test_odbc_password_escaped_without_disallowing_semicolon():
    assert _odbc_value('p;ass}word') == '{p;ass}}word}'
    ConnectionConfig(kind='tsql',host='localhost',user='user',password='p;ass}word',database='sales',port=1433).validate()


def test_connection_diagnostics_redacts_password_and_gives_specific_reason():
    cfg=ConnectionConfig(host='192.168.1.10',user='reader',password='never-expose-me',database='sales')
    msg=get_error_detail(RuntimeError('SSL certificate error using password=never-expose-me'), cfg)
    assert 'certificate' in msg.lower()
    assert 'never-expose-me' not in msg
    msg2=get_error_detail(RuntimeError('Access denied for user reader (1045)'),cfg)
    assert 'Login rejected' in msg2


def test_metadata_fetches_multiple_batches_not_limited_to_3000(monkeypatch):
    db=DatabaseGateway()
    class Cursor:
        def __init__(self): self.pos=0
        def execute(self,query,params=()): pass
        def fetchmany(self,n):
            if self.pos>=3501: return []
            count=min(n,3501-self.pos)
            result=[('test','tab_'+str((self.pos+i)//10),'col_'+str((self.pos+i)%10),
                     'VARCHAR(100)','YES','') for i in range(count)]
            self.pos+=count
            return result
        def close(self): pass
    class C:
        def cursor(self): return Cursor()
    tables=db._introspect(C(),ConnectionConfig(user='reader',database='test'))
    assert len(tables)>300
    assert sum(len(t.columns) for t in tables.values())==3501


def test_sql_server_driver_auto_detect(monkeypatch):
    class DummyODBC:
        def drivers(self):return ['ODBC Driver 17 for SQL Server']
    assert DatabaseGateway._sql_server_driver(DummyODBC()) == 'ODBC Driver 17 for SQL Server'
    with pytest.raises(RuntimeError):
        DatabaseGateway._sql_server_driver(DummyODBC(), 'ODBC Driver 18 for SQL Server')


def test_mysql_remote_strict_tls_and_legacy_modes_do_not_downgrade_silently(monkeypatch):
    import sys
    from types import SimpleNamespace
    captures=[]
    class Conn:
        def rollback(self): pass
        def close(self): pass
    def fake_connect(**kwargs):
        captures.append(kwargs)
        return Conn()
    monkeypatch.setitem(sys.modules,'pymysql',SimpleNamespace(connect=fake_connect))
    db=DatabaseGateway()
    cfg=ConnectionConfig(host='192.168.0.20',user='reader',database='loans',password='fake-pass')
    with db._connect(cfg): pass
    assert captures[-1]['ssl'] is not None
    cfg.tls_mode='legacy'
    with db._connect(cfg): pass
    assert captures[-1]['ssl'] is None
    cfg.tls_mode='trust'
    with db._connect(cfg): pass
    assert captures[-1]['ssl'] is not None
    assert captures[-1]['ssl'].check_hostname is False


def test_sql_server_odbc_connection_can_use_quoted_password(monkeypatch):
    import sys
    from types import SimpleNamespace
    saved=[]
    class Conn:
        def rollback(self): pass
        def close(self): pass
    class FakeODBC:
        def drivers(self): return ['ODBC Driver 17 for SQL Server']
        def connect(self, connection_string, **kwargs):
            saved.append(connection_string)
            return Conn()
    monkeypatch.setitem(sys.modules,'pyodbc',FakeODBC())
    db=DatabaseGateway()
    cfg=ConnectionConfig(kind='tsql',host='10.10.2.5',user='reader',database='sales',port=1433,password='pw;with}brace')
    with db._connect(cfg): pass
    assert 'ODBC Driver 17 for SQL Server' in saved[-1]
    assert 'PWD={pw;with}}brace}' in saved[-1]
    assert 'Encrypt=yes' in saved[-1] and 'TrustServerCertificate=no' in saved[-1]


def test_self_signed_mysql_tls_error_has_actionable_safer_guidance():
    cfg = ConnectionConfig(
        kind='mysql', host='10.0.1.201', user='readonly', password='secret-for-test',
        database='internal', port=3306,
    )
    error = RuntimeError("(2003, Can't connect to MySQL server on '10.0.1.201' (SSL: CERTIFICATE_VERIFY_FAILED) certificate verify failed: self-signed certificate in certificate chain)")
    tip = get_error_detail(error, cfg)
    assert 'Use CA certificate from IT' in tip
    assert 'Internal TLS (unverified certificate)' in tip
    assert 'secret-for-test' not in tip


def test_ca_certificate_file_validation(tmp_path):
    import pytest
    ca = tmp_path / 'company_ca.pem'
    ca.write_text('placeholder test CA; never used for a real connection')
    cfg = ConnectionConfig(host='10.0.1.201', user='readonly', database='internal', ca_file=str(ca))
    cfg.validate()
    cfg.ca_file = str(tmp_path / 'missing.pem')
    with pytest.raises(ValueError, match='CA certificate file not found'):
        cfg.validate()
