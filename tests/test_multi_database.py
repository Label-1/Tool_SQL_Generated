"""Multiple databases/schemas on ONE connection; metadata-only and SQL read-only."""
from __future__ import annotations

from vsql.database import ConnectionConfig, DatabaseGateway
from vsql.models import Column, Filter, Projection, QueryPlan, Relation, Table, table_key
from vsql.security import check_generated_sql
from vsql.sql_builder import SQLBuilder, quote_table


class MySQLCursor:
    def __init__(self, conn):
        self.conn = conn
        self.loaded = False
    def execute(self, sql, params=()):
        self.conn.commands.append((sql, params))
    def fetchmany(self, n):
        if self.loaded:
            return []
        self.loaded = True
        return [
            ('data_loan', 'loans', 'ref', 'varchar(13)', 'NO', ''),
            ('data_loan', 'loans', 'amount', 'decimal(12,2)', 'NO', ''),
            ('label_idcard_mobile', 'payments', 'id', 'varchar(13)', 'NO', ''),
            ('label_idcard_mobile', 'payments', 'เพศ', 'char(1)', 'YES', ''),
            ('label_idcard_mobile', 'payments', 'ชื่อ', 'varchar(100)', 'YES', ''),
        ]
    def close(self):
        pass


class MySQLConn:
    def __init__(self):
        self.commands=[]
    def cursor(self):
        return MySQLCursor(self)


def test_mysql_lists_all_accessible_databases_with_one_connection():
    db = DatabaseGateway()
    conn = MySQLConn()
    cfg = ConnectionConfig(database='data_loan', user='readonly')
    tables = db._introspect(conn, cfg)
    assert len(tables) == 2
    assert table_key('data_loan', 'loans') in tables
    assert table_key('label_idcard_mobile', 'payments') in tables
    sql = conn.commands[0][0].lower()
    assert 'information_schema.columns' in sql
    assert 'table_schema = ' not in sql
    assert 'update ' not in sql and 'delete ' not in sql


def test_cross_mysql_database_join_uses_qualified_names_and_keeps_filter():
    tables = DatabaseGateway()._introspect(MySQLConn(), ConnectionConfig(database='data_loan', user='readonly'))
    a = table_key('data_loan', 'loans')
    b = table_key('label_idcard_mobile', 'payments')
    p=QueryPlan(dialect='mysql')
    p.add_node(tables[a]); p.add_node(tables[b])
    p.base=b
    p.projections = [Projection(b, 'ชื่อ')]
    p.joins = [Relation(a, 'ref', b, 'id', 'EXISTS')]
    p.filters = [Filter(b, 'เพศ', '=', 'M')]
    sql = SQLBuilder(p, tables).build().sql
    assert 'FROM `label_idcard_mobile`.`payments`' in sql
    assert 'FROM `data_loan`.`loans`' in sql
    assert "`เพศ` = 'M'" in sql
    assert check_generated_sql(sql).safe


class ServerCursor:
    def __init__(self, conn):
        self.conn = conn
        self.current = None
        self.used = set()
        self.timeout = None
    def execute(self, sql, params=()):
        self.conn.commands.append(sql)
        self.current = 'catalog' if 'sys.databases' in sql else ('archive' if '[archive]' in sql else 'sales')
    def fetchall(self):
        return [('archive',), ('sales',)]
    def fetchmany(self,n):
        if self.current in self.used:
            return []
        self.used.add(self.current)
        if self.current == 'archive':
            return [('dbo','invoices','id','int','NO',''), ('dbo','invoices','name','varchar','YES','')]
        return [('dbo','invoices','id','int','NO',''), ('dbo','invoices','total','int','YES','')]
    def close(self):
        pass


class ServerConn:
    def __init__(self): self.commands=[]
    def cursor(self): return ServerCursor(self)


def test_sql_server_lists_multi_database_and_renders_three_part_tables():
    db=DatabaseGateway()
    conn=ServerConn()
    tables=db._introspect(conn,ConnectionConfig(kind='tsql',host='localhost',user='reader',database='sales',port=1433))
    assert len(tables)==2
    assert table_key('dbo','invoices','sales') in tables
    assert table_key('dbo','invoices','archive') in tables
    assert tables[table_key('dbo','invoices','archive')].qualified_name == 'archive.dbo.invoices'
    assert quote_table(table_key('dbo','invoices','archive'),'tsql') == '[archive].[dbo].[invoices]'
    assert all(query.lower().lstrip().startswith('select') for query in conn.commands)
    plan=QueryPlan(dialect='tsql')
    a=table_key('dbo','invoices','sales'); b=table_key('dbo','invoices','archive')
    plan.add_node(tables[a]); plan.add_node(tables[b])
    plan.joins=[Relation(a,'id',b,'id','INNER')]
    plan.projections=[Projection(a,'id'),Projection(b,'name')]
    sql=SQLBuilder(plan,tables).build().sql
    assert '[sales].[dbo].[invoices]' in sql
    assert '[archive].[dbo].[invoices]' in sql
    assert check_generated_sql(sql).safe


class PGCursor:
    def __init__(self,conn): self.conn=conn; self.used=False
    def execute(self,sql,params=()):self.conn.commands.append(sql)
    def fetchmany(self,n):
        if self.used:return []
        self.used=True
        return [('public','clients','id','text','NO',''),('reporting','payments','id','text','NO','')]
    def close(self):pass
class PGConn:
    def __init__(self):self.commands=[]
    def cursor(self):return PGCursor(self)


def test_postgres_shows_all_schemas_in_connected_database_only():
    db=DatabaseGateway(); conn=PGConn()
    cfg=ConnectionConfig(kind='postgres',database='company',host='localhost',user='reader',port=5432)
    tables=db._introspect(conn,cfg)
    assert len(tables)==2
    a=table_key('public','clients'); b=table_key('reporting','payments')
    assert a in tables and b in tables
    assert quote_table(a,'postgres')=='"public"."clients"'
    assert quote_table(b,'postgres')=='"reporting"."payments"'
    assert 'table_catalog = current_database()' in conn.commands[0]


def test_sql_server_escapes_catalog_name_in_metadata_and_sql():
    key=table_key('dbo','sensitive','sales];DROP TABLE x;--')
    assert quote_table(key,'tsql')=='[sales]];DROP TABLE x;--].[dbo].[sensitive]'
    assert 'DROP' not in quote_table(key,'tsql').replace('[sales]];DROP TABLE x;--]','')
