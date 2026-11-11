> **v0.4 Scope expansion:** On MySQL / SQL Server, Metadata browsing now spans other databases on the **same server instance** which the login account can discover. This does NOT give new privileges; use a dedicated account with SELECT permissions only on explicitly approved tables and databases. Cross-database preview still runs through the original SQL validator + read-only database policy, and any denial remains a database authorization error. PostgreSQL remains limited to the connected database and its permitted schemas. Metadata names are still internal company information; Local AI is metadata-only.

# Visual SQL Studio — Security Design / นโยบายความปลอดภัย

## สิ่งที่ต้องป้องกัน

- ห้ามเปลี่ยนข้อมูล/โครงสร้าง Database ต้นฉบับโดยเด็ดขาดผ่านตัวโปรแกรม
- ห้ามส่งข้อมูลลูกค้าจริง รหัสผ่าน หรือผลลัพธ์ Preview เข้า Local AI
- ห้ามส่ง Metadata/Prompt ออกไป Cloud AI โดยไม่ได้รับอนุญาต
- Query ใหม่ต้องไม่รับ Context ของ Query ก่อนหน้า

## Trust boundaries

1. **UI / Visual Query Plan:** ผู้ใช้เลือก Table/Column/Filter; จำกัดรายการตาม Metadata ที่ได้รับอนุญาต
2. **Local AI:** รับเฉพาะ Metadata ที่เกี่ยวข้อง + Prompt + แผน Query ของ Workspace ปัจจุบัน (ไม่มี Result Rows/Credentials); โมเดลคืน Plan JSON ซึ่งถือว่าไม่เชื่อถือและต้อง Validate ก่อน
3. **SQL Compiler:** สร้าง SQL เองจาก Typed QueryPlan เท่านั้น; ผู้ใช้ไม่สามารถส่ง Raw SQL เพื่อ Preview โดยตรง
4. **Security Gateway:** ตรวจชนิด QueryPlan, ชื่อ Table/Column ตาม Metadata, ตรวจ SQL ทั้งข้อความ และตรวจ AST ด้วย SQLGlot ก่อน Preview ฐานข้อมูลจริง
5. **Database:** เชื่อมต่อด้วยบัญชี SELECT-only ที่จำกัดสิทธิ์จริงและ TLS; มี Row Limit + Timeout และใช้ read-only transaction เมื่อ Driver/Server รองรับ
6. **Clipboard / Workspace:** คัดลอก SQL ตามคำสั่งผู้ใช้; ไฟล์ .vsql ไม่เก็บ Credentials/Preview Rows แต่ **ยังไม่เข้ารหัส** และมี Filter Values

## สิ่งที่โปรแกรมทำจริง

- There is no bundled demo database. Program starts disconnected; Test Connection only runs `SELECT 1`, and Connect reads accessible metadata.
- MySQL: verifies server TLS certificate for remote hosts (localhost is an explicit exception), uses `START TRANSACTION READ ONLY` for Preview and rolls back afterward.
- PostgreSQL: remote hosts use `verify-full` TLS; localhost is an explicit exception. Connections use `default_transaction_read_only=on` and a statement timeout.
- SQL Server: ใช้ ODBC 18 หรือ 17 `Encrypt=yes;TrustServerCertificate=no;ApplicationIntent=ReadOnly`, Query Timeout **แต่ ApplicationIntent ไม่ใช่สิทธิ์ฐานข้อมูล**; ต้องใช้บัญชี SELECT-only ของ DBA จริง
- จำกัด Preview 100 แถว; ไม่มีปุ่ม Execute SQL อิสระ
- เปิด Ollama ผ่าน `http://127.0.0.1:11434` โดยไม่ใช้ Proxy, เช็คชื่อโมเดลจากรายการที่ติดตั้งในเครื่องและปฏิเสธชื่อโมเดลที่ระบุ Cloud ทั่วไป
- AI Model ถูกโหลดเฉพาะเมื่อสั่งใช้งาน, `keep_alive=0` เพื่อลดการค้าง RAM หลังตอบ
- New Query สร้าง QueryPlan และ Prompt ใหม่; Save Workspace ไม่บันทึก User Context

## สิ่งที่ต้องให้ DBA / IT ทำก่อนใช้ Production

- ตรวจว่าบัญชี DB **ไม่มีสิทธิ์ INSERT/UPDATE/DELETE/DDL/EXECUTE** รวมทั้ง Stored Procedure, User-defined Function และ Database Link ที่อาจมี Side Effect
- จัดการ TLS CA / Certificate อย่างถูกต้อง และจำกัด Network ACL ไปยัง Host/Port ที่อนุมัติ
- ยืนยันว่าไม่มี Firewall Egress จาก Ollama ไปยัง Cloud (พิจารณาแยก Firewall Rule ตาม Process)
- จำกัด Local Ollama Listener ให้เป็น Loopback; ตรวจ Telemetry/Log/Crash Dump ของ Runtime ทั้งหมด
- กำหนดสิทธิ์ Windows/Folder/Clipboard History/Disk Encryption (BitLocker หรือเทียบเท่า)
- ตรวจสอบการโยกย้ายไฟล์ `.vsql` และการใช้งานเครื่องส่วนตัวให้สอดคล้องนโยบายบริษัท
- ทดสอบ SQL Injection, Multi-statement, Stored Procedure/Function, Schema Changes, Resource Limits, Prompt Injection และข้อมูลที่อ่านได้เกินสิทธิ์บน Database ทดสอบ
- ทดสอบ Traffic ที่ออกจากเครื่องด้วยเครื่องมือเครือข่าย ก่อนอนุญาต Production

## ข้อจำกัดที่ต้องบอกผู้ใช้ตรง ๆ

- การอ่านข้อมูลจาก Database ยังอาจสร้าง Load/Lock ตาม Engine, View และ Query Plan; การมี LIMIT 100 **ไม่ได้รับประกันว่าเซิร์ฟเวอร์จะประมวลผลเพียง 100 แถว**
- SQL Parser อย่างเดียวไม่สามารถพิสูจน์ผลข้างเคียงของทุก View/UDF ได้; บัญชีฐานข้อมูลที่จำกัดสิทธิ์เป็นสิ่งจำเป็น
- Local AI ไม่ได้แปลว่าไม่มีความเสี่ยงข้อมูลรั่วโดยอัตโนมัติ หาก Runtime/OS ยังสามารถเชื่อมต่อ Internet ได้
- `.vsql` เป็น JSON และยังไม่เข้ารหัส; อย่าใส่ Secret ลงใน Filter และอย่าแชร์ไฟล์ออกนอกช่องทางอนุมัติ
- ไม่รับประกันความปลอดภัยจาก DB บน Production หากผู้ใช้ป้อนบัญชี Admin/Write-enabled แทน SELECT-only

## v0.2 Connection and UI changes

- All five connection fields and the database type selector appear in one page. No modal connection dialogs.
- **Test** is mandatory before **Connect**; changing any field invalidates the test. A successful login test does **not** validate account privileges.
- Real data Preview is read-only and only executed from the trusted query compiler, never directly from local AI or a free-form SQL editor.
- Query messages go to the local-only Ollama process; database rows and passwords are excluded. Model list selection is inline above the Canvas.
- No schema/connection password or customer result is persisted in `.vsql` workspaces; SQL/filter values can still contain sensitive information and must be handled as corporate data.
- Use IT/DBA-issued SELECT-only permissions, approved network access and verified TLS for remote hosts; GUI restrictions alone are not a security boundary.

## v0.3 - Connection compatibility, consent and privacy

- The default for a remote server is **verified TLS**. Compatibility modes are not activated automatically on a connection error.
- The same-window connection form includes a collapsed Advanced panel for explicitly selecting **encrypted but unverified TLS** or **legacy unencrypted LAN**. Either selection requires an acknowledgment before Test Connection.
- Encrypted but unverified connections protect against passive observation but NOT active server impersonation / MITM. Prefer the correct corporate CA and hostname.
- The legacy mode sends data without full-session encryption and may leak credentials/results; it should be restricted to an IT-authorized isolated network, never Internet or public Wi-Fi.
- If the company requires verified TLS for database connections, IT should disable both compatibility modes before deploying the program.
- Database connection errors are shortened and scrubbed of the password before being shown in the interface. Avoid sharing screenshots that include any entered credentials.
- SQL generated by the Local AI is never executed directly; only compiler-generated SQL from validated plans can be previewed. The tool does not issue DML or DDL operations.
- Success of `SELECT 1` does **not** prove that a database account is read only; DBA approval of SELECT-only permissions remains mandatory.
- Metadata is now retrieved in batches (up to 100,000 columns); this can still consume resources and should be constrained at DB permissions if tables are huge.

## v0.3.1 - Self-signed certificate handling

- IT-supplied CA files can now be selected in Advanced. The default remains verified TLS for remote servers.
- A self-signed certificate error is not caused by incorrect credentials; it means the server's identity could not be verified. The correct fix is an IT-trusted CA and hostname/IP matching the certificate.
- Internal TLS with certificate verification disabled is an explicit, per-session, IT-approved exception; it still has a man-in-the-middle risk. Do not select Legacy LAN mode to solve a certificate error without separate security approval.
- Even after successful Test Connection, application login does NOT prove account is read-only. Provision least-privilege SELECT-only accounts on the server.
