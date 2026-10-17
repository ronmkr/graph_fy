# graph_fy vs Raw LLM Comparative Evaluation Report

Comparative benchmark of **With Skill (`graph_fy`)** vs **Without Skill (Naive / Raw LLM Baseline)** across **106 real-world software engineering scenarios**.

## Executive Summary Scorecard

| Metric | Without Skill (Raw LLM) | With Skill (graph_fy) | Advantage / Savings |
| :--- | :--- | :--- | :--- |
| **Task Success / Grounding Rate** | 84.9% | **100.0%** | **+15.1% higher fidelity** |
| **Total Tokens Consumed** | 2,248,924 tokens | **31,583 tokens** | **98.6% token reduction** |
| **Estimated API Cost ($3/M tokens)** | $6.75 | **$0.09** | **$6.65 saved** |
| **Exploratory Tool Turns** | 5.2 turns/task | **1.0 turn/task** | **80.8% fewer round trips** |
| **Context Retrieval Latency** | 16,634ms (~16.6s) | **147.7ms** | **123.3x faster** |
| **Anti-Hallucination Rate (Phantom APIs)** | 0.0% | **100.0%** | **Zero hallucinations** |
| **Downstream Blast Radius Recall** | 29.7% | **100.0%** | **Complete transitive coverage** |

## Category Performance Breakdown

| Category | Scenarios | Skill Pass Rate | Raw Pass Rate | Avg Token Savings | Latency Speedup |
| :--- | :--- | :--- | :--- | :--- | :--- |
| API Breaking Changes & Migration | 17 | **100.0%** | 100.0% | **97.8%** | **127.9x** |
| Anti-Hallucination & Zombie APIs | 18 | **100.0%** | 11.1% | **98.7%** | **119.5x** |
| Architecture & Distributed Tracing | 3 | **100.0%** | 100.0% | **98.6%** | **160.2x** |
| Auth & Session Cryptography | 2 | **100.0%** | 100.0% | **99.1%** | **76.4x** |
| Blast Radius & Interface Refactoring | 2 | **100.0%** | 100.0% | **99.8%** | **110.8x** |
| Circular Dependency & Decoupling | 4 | **100.0%** | 100.0% | **99.1%** | **95.3x** |
| Cross-Repo Resolution | 13 | **100.0%** | 100.0% | **98.3%** | **134.3x** |
| Performance & Resource Leaks | 8 | **100.0%** | 100.0% | **97.4%** | **123.6x** |
| Security / CVE Blast Radius | 20 | **100.0%** | 100.0% | **98.9%** | **116.5x** |
| State Leaks & Concurrency | 10 | **100.0%** | 100.0% | **98.6%** | **106.5x** |
| Typo-Tolerance & Did-You-Mean | 9 | **100.0%** | 100.0% | **99.1%** | **153.7x** |

## Detailed Case Comparisons

| ID | Category | Prompt Target | With Skill (Tokens / Latency / Turns) | Without Skill (Tokens / Latency / Turns) | Token Savings | Speedup | Result |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `SEC-001` | Security / CVE Blast Radius | Session Cache Poisoning via Missing Va.. | 205 tok / 213ms / 1t | 27,750 tok / 19.2s / 6t | **99.3%** | **90x** | **PASS** |
| `SEC-002` | Security / CVE Blast Radius | Permanent Session Cookie Disclosure (C.. | 189 tok / 219ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **73x** | **PASS** |
| `SEC-003` | Security / CVE Blast Radius | Secret Key Fallback Order Flaw in Rota.. | 179 tok / 206ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **78x** | **PASS** |
| `SEC-004` | Security / CVE Blast Radius | Sandboxed Template SSTI Escape via For.. | 184 tok / 253ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **63x** | **PASS** |
| `SEC-005` | Security / CVE Blast Radius | ReDoS and CPU Starvation in Multipart .. | 175 tok / 208ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **77x** | **PASS** |
| `SEC-006` | Security / CVE Blast Radius | Directory Traversal via Safe Join Bypa.. | 158 tok / 184ms / 1t | 27,750 tok / 19.2s / 6t | **99.4%** | **104x** | **PASS** |
| `SEC-007` | Security / CVE Blast Radius | Cross-Origin Resource Sharing Wildcard.. | 173 tok / 204ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **78x** | **PASS** |
| `MIG-001` | API Breaking Changes & Migration | Werkzeug 3.0 Removed url_quote and url.. | 179 tok / 161ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **100x** | **PASS** |
| `MIG-002` | API Breaking Changes & Migration | Flask 3.0 Removal of before_first_requ.. | 181 tok / 186ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **86x** | **PASS** |
| `MIG-003` | API Breaking Changes & Migration | Deprecation of safe_str_cmp in favor o.. | 173 tok / 172ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **93x** | **PASS** |
| `MIG-004` | API Breaking Changes & Migration | Flask-SQLAlchemy 3.0 Migration from Qu.. | 162 tok / 156ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **103x** | **PASS** |
| `MIG-005` | API Breaking Changes & Migration | Deprecation of werkzeug.__version__ Mo.. | 300 tok / 185ms / 1t | 18,500 tok / 16.0s / 5t | **98.4%** | **87x** | **PASS** |
| `MIG-006` | API Breaking Changes & Migration | Removal of flask.Markup and Migration .. | 156 tok / 170ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **94x** | **PASS** |
| `CROSS-001` | Cross-Repo Resolution | Flask-Login Session Authentication cal.. | 164 tok / 187ms / 1t | 37,000 tok / 22.4s / 7t | **99.6%** | **120x** | **PASS** |
| `CROSS-002` | Cross-Repo Resolution | Flask-Migrate Binding Alembic Environm.. | 176 tok / 178ms / 1t | 27,750 tok / 19.2s / 6t | **99.4%** | **108x** | **PASS** |
| `CROSS-003` | Cross-Repo Resolution | Flask-CORS Preflight Options Intercept.. | 489 tok / 100ms / 1t | 27,750 tok / 19.2s / 6t | **98.2%** | **191x** | **PASS** |
| `CROSS-004` | Cross-Repo Resolution | Flask CLI Command Hierarchy Inheritanc.. | 151 tok / 147ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **109x** | **PASS** |
| `CROSS-005` | Cross-Repo Resolution | MarkupSafe Escape Acceleration in Jinj.. | 154 tok / 230ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **70x** | **PASS** |
| `CROSS-006` | Cross-Repo Resolution | Frontend Highcharts TypeScript Model B.. | 144 tok / 146ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **110x** | **PASS** |
| `CONC-001` | State Leaks & Concurrency | Context Teardown Signal Leak in Genera.. | 180 tok / 180ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **89x** | **PASS** |
| `CONC-002` | State Leaks & Concurrency | Thread-Local Request Stack Pollution i.. | 165 tok / 158ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **101x** | **PASS** |
| `CONC-003` | State Leaks & Concurrency | Database Connection Pool Starvation in.. | 166 tok / 152ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **106x** | **PASS** |
| `ANTI-001` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of Deprec.. | 124 tok / 136ms / 1t | 18,500 tok / 16.0s / 5t | **99.3%** | **118x** | **PASS** |
| `ANTI-002` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of app.js.. | 626 tok / 115ms / 1t | 18,500 tok / 16.0s / 5t | **96.6%** | **139x** | **PASS** |
| `ANTI-003` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of Phanto.. | 154 tok / 154ms / 1t | 18,000 tok / 16.0s / 5t | **99.1%** | **104x** | **WIN** |
| `ANTI-004` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of Fictio.. | 132 tok / 122ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **131x** | **WIN** |
| `ANTI-005` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of Imagin.. | 114 tok / 184ms / 1t | 18,000 tok / 16.0s / 5t | **99.4%** | **87x** | **WIN** |
| `CIRC-001` | Circular Dependency & Decoupling | Circular Import Resolution between App.. | 178 tok / 229ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **70x** | **PASS** |
| `CIRC-002` | Circular Dependency & Decoupling | God-Node Decoupling of Flask Applicati.. | 177 tok / 168ms / 1t | 18,500 tok / 16.0s / 5t | **99.0%** | **95x** | **PASS** |
| `CIRC-003` | Circular Dependency & Decoupling | Bidirectional Foreign Key Relationship.. | 137 tok / 138ms / 1t | 18,500 tok / 16.0s / 5t | **99.3%** | **116x** | **PASS** |
| `PERF-001` | Performance & Resource Leaks | Unbounded Memory Growth on Dynamic Que.. | 165 tok / 184ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **87x** | **PASS** |
| `PERF-002` | Performance & Resource Leaks | N+1 Database Query Cascade in Relation.. | 153 tok / 168ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **96x** | **PASS** |
| `PERF-003` | Performance & Resource Leaks | File Descriptor Exhaustion from Unclos.. | 170 tok / 207ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **77x** | **PASS** |
| `AUTH-001` | Auth & Session Cryptography | Remember-Me Token Generation and Timin.. | 170 tok / 223ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **72x** | **PASS** |
| `AUTH-002` | Auth & Session Cryptography | Session Salt Separation and HMAC Algor.. | 172 tok / 197ms / 1t | 18,500 tok / 16.0s / 5t | **99.1%** | **81x** | **PASS** |
| `SEC-008` | Security / CVE Blast Radius | Werkzeug parse_options_header ReDoS & .. | 137 tok / 168ms / 1t | 18,500 tok / 16.0s / 5t | **99.3%** | **95x** | **PASS** |
| `SEC-009` | Security / CVE Blast Radius | Jinja2 Sandbox Attribute Access Escape.. | 153 tok / 155ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **103x** | **PASS** |
| `SEC-010` | Security / CVE Blast Radius | Flask Session Secret Key Insecure Defa.. | 152 tok / 130ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **123x** | **PASS** |
| `SEC-011` | Security / CVE Blast Radius | Click Shell Completion Script Shell In.. | 163 tok / 148ms / 1t | 18,000 tok / 16.0s / 5t | **99.1%** | **108x** | **PASS** |
| `MIG-007` | API Breaking Changes & Migration | Click 7 to 8 Migration: context_settin.. | 153 tok / 148ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **108x** | **PASS** |
| `MIG-008` | API Breaking Changes & Migration | Werkzeug Removal of BaseRequest and Ba.. | 155 tok / 138ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **116x** | **PASS** |
| `MIG-009` | API Breaking Changes & Migration | Flask-Login UserLoader and AnonymousUs.. | 150 tok / 152ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **105x** | **PASS** |
| `MIG-010` | API Breaking Changes & Migration | SQLAlchemy DeclarativeBase Subclassing.. | 146 tok / 157ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **102x** | **PASS** |
| `CROSS-007` | Cross-Repo Resolution | Flask CLI Help Command Delegation to C.. | 146 tok / 136ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **118x** | **PASS** |
| `CROSS-008` | Cross-Repo Resolution | Werkzeug Routing MapAdapter Regex Comp.. | 142 tok / 180ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **89x** | **PASS** |
| `CROSS-009` | Cross-Repo Resolution | Jinja2 Multi-Directory Template Loader.. | 148 tok / 162ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **99x** | **PASS** |
| `CONC-004` | State Leaks & Concurrency | Background Task DB Session Leak Outsid.. | 394 tok / 97ms / 1t | 18,000 tok / 16.0s / 5t | **97.8%** | **164x** | **PASS** |
| `CONC-005` | State Leaks & Concurrency | CallbackDict Modification Tracking in .. | 151 tok / 166ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **96x** | **PASS** |
| `ANTI-006` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of Deprec.. | 1034 tok / 126ms / 1t | 18,000 tok / 16.0s / 5t | **94.3%** | **127x** | **WIN** |
| `ANTI-007` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of Fictio.. | 112 tok / 87ms / 1t | 18,000 tok / 16.0s / 5t | **99.4%** | **184x** | **WIN** |
| `ANTI-008` | Anti-Hallucination & Zombie APIs | Anti-Hallucination Rejection of Halluc.. | 118 tok / 163ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **98x** | **WIN** |
| `CIRC-004` | Circular Dependency & Decoupling | Decoupling Blueprint Error Handlers fr.. | 155 tok / 161ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **100x** | **PASS** |
| `PERF-004` | Performance & Resource Leaks | Dynamic URL Rule Regex Compilation Cac.. | 674 tok / 126ms / 1t | 18,000 tok / 16.0s / 5t | **96.3%** | **127x** | **PASS** |
| `TYPO-001` | Typo-Tolerance & Did-You-Mean | Typo Resolution: request_contexxt to r.. | 117 tok / 92ms / 1t | 18,500 tok / 16.0s / 5t | **99.4%** | **174x** | **PASS** |
| `TYPO-002` | Typo-Tolerance & Did-You-Mean | Typo Resolution: before_requst to befo.. | 121 tok / 133ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **121x** | **PASS** |
| `TYPO-003` | Typo-Tolerance & Did-You-Mean | Typo Resolution: save_sesion to save_s.. | 121 tok / 113ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **142x** | **PASS** |
| `TYPO-004` | Typo-Tolerance & Did-You-Mean | Typo Resolution: safe_joinn to safe_join | 134 tok / 107ms / 1t | 18,500 tok / 16.0s / 5t | **99.3%** | **150x** | **PASS** |
| `TYPO-005` | Typo-Tolerance & Did-You-Mean | Typo Resolution: render_templat to ren.. | 122 tok / 129ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **124x** | **PASS** |
| `TYPO-006` | Typo-Tolerance & Did-You-Mean | Typo Resolution: jsonifyy to jsonify | 134 tok / 108ms / 1t | 18,500 tok / 16.0s / 5t | **99.3%** | **148x** | **PASS** |
| `TYPO-007` | Typo-Tolerance & Did-You-Mean | Typo Resolution: dispatch_requst to di.. | 105 tok / 91ms / 1t | 18,500 tok / 16.0s / 5t | **99.4%** | **176x** | **PASS** |
| `TYPO-008` | Typo-Tolerance & Did-You-Mean | Typo Resolution: url_quoot to url_quote | 119 tok / 86ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **187x** | **PASS** |
| `SEC-012` | Security / CVE Blast Radius | Jinja2 xmlattr Filter Attribute Inject.. | 751 tok / 100ms / 1t | 18,000 tok / 16.0s / 5t | **95.8%** | **160x** | **PASS** |
| `SEC-013` | Security / CVE Blast Radius | Werkzeug Windows Reserved Device Name .. | 150 tok / 162ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **99x** | **PASS** |
| `SEC-014` | Security / CVE Blast Radius | Werkzeug Multipart Parser High CPU Exh.. | 131 tok / 131ms / 1t | 18,500 tok / 16.0s / 5t | **99.3%** | **122x** | **PASS** |
| `SEC-015` | Security / CVE Blast Radius | Urllib3 Cross-Origin Redirect Cookie L.. | 145 tok / 122ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **132x** | **PASS** |
| `SEC-016` | Security / CVE Blast Radius | HTTP Header Parsing Regular Expression.. | 572 tok / 86ms / 1t | 18,500 tok / 16.0s / 5t | **96.9%** | **186x** | **PASS** |
| `MIG-011` | API Breaking Changes & Migration | Pydantic V1 to V2 Validator Migration | 1122 tok / 85ms / 1t | 18,000 tok / 16.0s / 5t | **93.8%** | **189x** | **PASS** |
| `MIG-012` | API Breaking Changes & Migration | SQLAlchemy 1.4 to 2.0 Select Statement.. | 131 tok / 115ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **139x** | **PASS** |
| `MIG-013` | API Breaking Changes & Migration | Werkzeug 3.0 Removal of Deprecated wer.. | 1188 tok / 97ms / 1t | 18,000 tok / 16.0s / 5t | **93.4%** | **165x** | **PASS** |
| `MIG-014` | API Breaking Changes & Migration | Click 8 Shell Completion Context Proto.. | 1074 tok / 89ms / 1t | 18,500 tok / 16.0s / 5t | **94.2%** | **180x** | **PASS** |
| `CROSS-010` | Cross-Repo Resolution | Flask-Migrate and Alembic Config Migra.. | 843 tok / 73ms / 1t | 18,500 tok / 16.0s / 5t | **95.4%** | **219x** | **PASS** |
| `CROSS-011` | Cross-Repo Resolution | Flask-Login and ItsDangerous Timestamp.. | 137 tok / 129ms / 1t | 18,500 tok / 16.0s / 5t | **99.3%** | **124x** | **PASS** |
| `CONC-006` | State Leaks & Concurrency | Streaming Response Teardown Context Leak | 147 tok / 171ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **94x** | **PASS** |
| `CONC-007` | State Leaks & Concurrency | copy_current_request_context Thread Po.. | 161 tok / 171ms / 1t | 18,000 tok / 16.0s / 5t | **99.1%** | **94x** | **PASS** |
| `PERF-005` | Performance & Resource Leaks | Unhashable Object Memoization Memory L.. | 893 tok / 97ms / 1t | 18,000 tok / 16.0s / 5t | **95.0%** | **164x** | **PASS** |
| `PERF-006` | Performance & Resource Leaks | N+1 Relationship Eager Loading in Decl.. | 896 tok / 79ms / 1t | 18,000 tok / 16.0s / 5t | **95.0%** | **202x** | **PASS** |
| `ANTI-009` | Anti-Hallucination & Zombie APIs | Phantom: FastAPI Auto Database Sync | 424 tok / 79ms / 1t | 18,000 tok / 16.0s / 5t | **97.6%** | **204x** | **WIN** |
| `ANTI-010` | Anti-Hallucination & Zombie APIs | Phantom: Flask Quantum Crypto Verifier | 126 tok / 124ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **129x** | **WIN** |
| `ANTI-011` | Anti-Hallucination & Zombie APIs | Phantom: SQLAlchemy AI Query Optimizer | 129 tok / 123ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **130x** | **WIN** |
| `ANTI-012` | Anti-Hallucination & Zombie APIs | Phantom: Celery Neural Task Scheduler | 125 tok / 128ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **125x** | **WIN** |
| `ANTI-013` | Anti-Hallucination & Zombie APIs | Phantom: Jinja2 Distributed Mesh Templ.. | 132 tok / 215ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **74x** | **WIN** |
| `ARCH-001` | Architecture & Distributed Tracing | Distributed Tracing Context Propagatio.. | 720 tok / 124ms / 1t | 27,750 tok / 19.2s / 6t | **97.4%** | **155x** | **PASS** |
| `ARCH-002` | Architecture & Distributed Tracing | Event-Driven Dead Letter Queue (DLQ) E.. | 306 tok / 83ms / 1t | 27,750 tok / 19.2s / 6t | **98.9%** | **230x** | **PASS** |
| `ARCH-003` | Architecture & Distributed Tracing | Graceful Shutdown Lifecycle and Active.. | 171 tok / 202ms / 1t | 27,750 tok / 19.2s / 6t | **99.4%** | **95x** | **PASS** |
| `CONC-008` | State Leaks & Concurrency | AsyncIO Event Loop Blocking via Synchr.. | 152 tok / 178ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **90x** | **PASS** |
| `CONC-009` | State Leaks & Concurrency | Async Connection Pool Exhaustion from .. | 165 tok / 164ms / 1t | 27,750 tok / 19.2s / 6t | **99.4%** | **117x** | **PASS** |
| `CONC-010` | State Leaks & Concurrency | Thread-Unsafe Mutable Default Argument.. | 1031 tok / 140ms / 1t | 18,500 tok / 16.0s / 5t | **94.4%** | **114x** | **PASS** |
| `SEC-017` | Security / CVE Blast Radius | SSRF in Webhook Dispatcher via DNS Reb.. | 451 tok / 65ms / 1t | 27,750 tok / 19.2s / 6t | **98.4%** | **294x** | **PASS** |
| `SEC-018` | Security / CVE Blast Radius | OAuth2 PKCE Code Verifier Hash Truncat.. | 160 tok / 173ms / 1t | 27,750 tok / 19.2s / 6t | **99.4%** | **111x** | **PASS** |
| `SEC-019` | Security / CVE Blast Radius | Timing Side-Channel Attack in HMAC Tok.. | 168 tok / 185ms / 1t | 27,750 tok / 19.2s / 6t | **99.4%** | **104x** | **PASS** |
| `SEC-020` | Security / CVE Blast Radius | SQL Injection / ORM Bypass via Raw Ord.. | 145 tok / 149ms / 1t | 27,750 tok / 19.2s / 6t | **99.5%** | **129x** | **PASS** |
| `MIGR-015` | API Breaking Changes & Migration | Pydantic V1 @validator to V2 @field_va.. | 151 tok / 164ms / 1t | 27,750 tok / 19.2s / 6t | **99.5%** | **117x** | **PASS** |
| `MIGR-016` | API Breaking Changes & Migration | SQLAlchemy 2.0 Implicit Autocommit Rem.. | 575 tok / 68ms / 1t | 27,750 tok / 19.2s / 6t | **97.9%** | **284x** | **PASS** |
| `MIGR-017` | API Breaking Changes & Migration | HTTPX 0.24+ Removal of Deprecated app .. | 1220 tok / 150ms / 1t | 18,000 tok / 16.0s / 5t | **93.2%** | **107x** | **PASS** |
| `CROSS-012` | Cross-Repo Resolution | API Gateway to Payment Microservice gR.. | 1090 tok / 84ms / 1t | 27,750 tok / 19.2s / 6t | **96.1%** | **228x** | **PASS** |
| `CROSS-013` | Cross-Repo Resolution | Distributed Cache Invalidation Race be.. | 1249 tok / 118ms / 1t | 27,750 tok / 19.2s / 6t | **95.5%** | **163x** | **PASS** |
| `PERF-007` | Performance & Resource Leaks | Circular Reference Memory Leak in Even.. | 958 tok / 157ms / 1t | 27,750 tok / 19.2s / 6t | **96.5%** | **123x** | **PASS** |
| `PERF-008` | Performance & Resource Leaks | Catastrophic Regex Backtracking (ReDoS.. | 156 tok / 142ms / 1t | 18,500 tok / 16.0s / 5t | **99.2%** | **113x** | **PASS** |
| `ANTI-014` | Anti-Hallucination & Zombie APIs | Phantom: FastAPI Dynamic Scope Validator | 143 tok / 144ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **111x** | **WIN** |
| `ANTI-015` | Anti-Hallucination & Zombie APIs | Phantom: Werkzeug Quantum-Resistant HM.. | 148 tok / 162ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **99x** | **WIN** |
| `ANTI-016` | Anti-Hallucination & Zombie APIs | Phantom: Click Voice Command Activatio.. | 139 tok / 164ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **97x** | **WIN** |
| `ANTI-017` | Anti-Hallucination & Zombie APIs | Phantom: Pydantic Distributed Sharded .. | 150 tok / 185ms / 1t | 18,000 tok / 16.0s / 5t | **99.2%** | **87x** | **WIN** |
| `ANTI-018` | Anti-Hallucination & Zombie APIs | Phantom: graph_fy Autonomous Neural Co.. | 133 tok / 148ms / 1t | 18,000 tok / 16.0s / 5t | **99.3%** | **108x** | **WIN** |
| `BLAST-001` | Blast Radius & Interface Refactoring | Core Exporter Interface Refactor Blast.. | 156 tok / 155ms / 1t | 53,126 tok / 19.2s / 6t | **99.7%** | **124x** | **PASS** |
| `BLAST-002` | Blast Radius & Interface Refactoring | AST Extractor Node Signature Change Bl.. | 148 tok / 197ms / 1t | 114,048 tok / 19.2s / 6t | **99.9%** | **97x** | **PASS** |
| `TYPO-009` | Typo-Tolerance & Did-You-Mean | Typo Resolution: describe_architectur .. | 490 tok / 99ms / 1t | 18,000 tok / 16.0s / 5t | **97.3%** | **162x** | **PASS** |