# PatchPilot — Szczegółowy plan architektoniczny

---

## 1. Wizja i cel

PatchPilot to **agentlessowe** narzędzie CLI do bezpiecznego aktualizowania floty serwerów Linux przez SSH. Zamiast próbować być kolejnym Ansiblem, skupia się na jednym, trudnym problemie SRE/DevOps: **jak wdrożyć aktualizacje na dziesiątkach serwerów tak, aby jeden błąd nie położył całego środowiska**.

### Kluczowe założenia
- **Agentless** — zero oprogramowania na hostach docelowych. Wszystko dzieje się przez SSH + natywne narzędzia systemowe.
- **Strategiczne wdrażanie** — canary, batch, single. Nie aktualizujemy wszystkiego naraz.
- **Automatyczna ochrona** — snapshot przed zmianą, health check po zmianie, rollback przy awarii.
- **Idempotencja i odporność** — przerwane połączenie lub crash programu nie zostawiają systemu w stanie nieokreślonym.
- **Pełna widoczność** — audyt, metryki Prometheus, raporty JSON/terminalowe.

---

## 2. Stack technologiczny — uzasadnienie

| Warstwa | Wybór | Dlaczego |
|---------|-------|----------|
| Język | Python 3.11+ | Bogaty ekosystem async, łatwość czytania kodu przez rekruterów, natywne wsparcie dla SSH i sieci. |
| CLI | `click` | Standard w Pythonie, wsparcie dla grup komend, autogenerowanie `--help`, walidacja typów. |
| SSH | `asyncssh` | Asynchroniczne, wysokopoziomowe API, wspiera ED25519, sudo, proxy jump, multiplexing. |
| Baza danych | SQLite (MVP) / PostgreSQL (scale) | SQLite wystarcza na MVP bez dodatkowej infrastruktury. SQLAlchemy daje później drogę do Postgresa bez zmiany kodu. |
| ORM / DB | `sqlalchemy` 2.0+ z async driverami (`aiosqlite`, `asyncpg`) | Jednolite API, wsparcie dla transakcji, migracje przez Alembic (opcjonalnie). |
| Konfiguracja | YAML (`PyYAML`) + `pydantic` do walidacji | Czytelne dla administratorów, schemat walidowany przy starcie. |
| Daty / timezone | `python-dateutil` + `zoneinfo` (stdlib) | Timezone-aware maintenance windows. |
| Metryki | Plain text Prometheus format | Zero zależności. Wystarczy zapisać `.prom` file. |
| Logowanie | `structlog` | Strukturalne logi JSON, czytelne w Loki/ELK. |
| Testy | `pytest` + `pytest-asyncio` + `testcontainers` | Testy integracyjne z prawdziwymi kontenerami. |

### Decyzja: dlaczego nie Paramiko / Ansible / Fabric?
- **Paramiko** — synchroniczny, niskopoziomowy, wymaga więcej boilerplate przy async.
- **Ansible** — to framework do wszystkiego. PatchPilot ma być narzędziem do jednej rzeczy, ale robionej bezpieczniej.
- **Fabric** — nie daje async out-of-the-box.

---

## 3. Architektura ogólna i przepływ danych

PatchPilot działa jako **CLI na maszynie sterującej** (laptop / bastion / CI runner). Nie ma serwera ani agentów. Baza SQLite jest plikiem lokalnym.

```
┌──────────────────────────────────────────────────────────────┐
│                     Operator (CLI)                            │
│  patchpilot plan production                                  │
│  patchpilot deploy production --strategy canary              │
└────────────────────┬─────────────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────────────┐
│                  CLI Layer (click)                          │
│  • Walidacja argumentów                                      │
│  • Wczytanie inventory (YAML + Pydantic)                   │
│  • Wybór podkomendy                                        │
└────────────────────┬─────────────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────────────┐
│              Rollout Engine (core)                          │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐        │
│  │   Planner   │  │   Strategy   │  │   Executor   │        │
│  │  (dry-run)  │  │ (canary/     │  │ (async SSH   │        │
│  │             │  │  batch)      │  │  + steps)    │        │
│  └──────┬──────┘  └──────┬───────┘  └──────┬──────┘        │
│         └────────────────┘─────────────────┘                │
│                          │                                  │
│              ┌───────────▼───────────┐                      │
│              │     State Machine     │                      │
│              │  (transakcje + DB)    │                      │
│              └───────────┬───────────┘                      │
└──────────────────────────┼──────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
┌───────▼──────┐  ┌────────▼────────┐  ┌─────▼──────┐
│  SSH Engine  │  │  Subsystems     │  │   State    │
│  (asyncssh)  │  │  (packages/     │  │  (SQLite)  │
│              │  │   health/       │  │            │
│  • Pool      │  │   snapshots)    │  │  Locking   │
│  • Retry     │  │                 │  │  Recovery  │
│  • Sudo      │  │                 │  │  Audit     │
│  • Timeout   │  │                 │  │  Metrics   │
└──────┬───────┘  └────────┬────────┘  └────────────┘
       │                   │
       └─────────┬─────────┘
                 │
    ┌────────────┼────────────┐
    ▼            ▼            ▼
┌───────┐  ┌─────────┐  ┌─────────┐
│ Host 1 │  │ Host 2  │  │ Host N  │
│ Ubuntu │  │ Debian  │  │ Rocky   │
└────────┘  └─────────┘  └─────────┘
```

### Przepływ danych (deploy)
1. CLI wczytuje `inventory/production.yaml`.
2. `Planner` łączy się z każdym hostem (równolegle, max N) i wykonuje `dry-run` package manager.
3. Wyniki zapisuje w pamięci (nie w DB — to tylko plan).
4. Operator zatwierdza (lub `--auto-approve`).
5. `Rollout Engine` tworzy rekord w `rollouts` (z blokadą).
6. `Strategy` dzieli hosty na batchy / kanarka.
7. `Executor` iteruje batchami:
   - Dla każdego hosta: snapshot → update → (reboot) → health checks.
   - Po każdym kroku: zapis `rollout_steps` + `audit_events`.
   - Jeśli health check FAIL: zatrzymanie, rollback, aktualizacja stanu.
8. Po zakończeniu: raport, metryki `.prom`, finalny audyt.

---

## 4. Konfiguracja (Inventory YAML) — schemat i przykład

PatchPilot wymaga jednego pliku YAML per środowisko. Ścieżka domyślna: `~/.config/patchpilot/<env>.yaml` lub podana przez `--inventory`.

### Pełny przykład konfiguracji

```yaml
---
# =============================================================================
# PatchPilot Inventory Schema — production.yaml
# =============================================================================

metadata:
  name: production
  description: "Production API and database fleet"
  owner: sre-team@example.com

connection:
  # Globalne ustawienia SSH (można nadpisać per-host)
  ssh_user: deploy
  ssh_key_path: ~/.ssh/patchpilot_ed25519
  ssh_timeout: 30
  sudo: true
  sudo_password: null  # jeśli null, zakładamy NOPASSWD w sudoers
  parallel_limit: 5    # max równoległych połączeń SSH
  retry:
    max_attempts: 3
    backoff_seconds: 5

hosts:
  - name: api-01
    address: 10.10.0.11
    role: api
    # Nadpisanie globalne:
    ssh_user: ubuntu
    tags:
      - canary-eligible
      - nginx

  - name: api-02
    address: 10.10.0.12
    role: api
    tags:
      - nginx

  - name: api-03
    address: 10.10.0.13
    role: api
    tags:
      - nginx

  - name: db-01
    address: 10.10.0.20
    role: database
    # Bazy danych nie mogą być kanarkiem
    tags:
      - no-canary

strategy:
  # Dostępne: canary | batch | single
  type: canary
  canary:
    count: 1
    # Host musi mieć tag 'canary-eligible' aby być wybranym jako kanarek
    tag_filter: canary-eligible
  batch:
    size: 2
    # Czekaj na zakończenie całego batcha przed następnym?
    wait_for_batch: true
    # Maksymalny czas oczekiwania na health check całego batcha
    health_check_timeout: 300

health_checks:
  # Globalne checki (aplikowane do wszystkich ról)
  global:
    - type: systemd
      service: ssh.service
      state: active

  # Per-rola
  per_role:
    api:
      - type: http
        url: http://localhost:8080/health
        method: GET
        expected_status: 200
        timeout: 10
        retries: 3
      - type: systemd
        service: nginx.service
        state: active
      - type: journal
        service: nginx.service
        forbidden_patterns:
          - "OutOfMemory"
          - "upstream prematurely closed connection"
        lookback_seconds: 60
    database:
      - type: tcp
        host: localhost
        port: 5432
        timeout: 5
      - type: command
        command: "pg_isready -q"
        expected_exit_code: 0
        timeout: 5

snapshot:
  # Jaką technologię wymusić? 'auto' = wykrycie na hoście
  preferred: auto
  # Jeśli snapshot niemożliwy — czy kontynuować z ostrzeżeniem czy przerwać?
  on_unavailable: warn   # warn | abort
  # Maksymalny czas oczekiwania na utworzenie snapshotu
  timeout: 120

maintenance:
  timezone: Europe/Warsaw
  windows:
    - start: "23:00"
      end: "02:00"
      days:
        - saturday
        - sunday
    - start: "02:00"
      end: "04:00"
      days:
        - wednesday
  # Czy planowanie może odbywać się poza oknem, a deploy tylko w oknie?
  plan_outside_window: true
  deploy_outside_window: false

audit:
  enabled: true
  # Ścieżka do pliku bazy SQLite z audytem i stanem rolloutów
  db_path: ~/.local/share/patchpilot/rollouts.db
  # Hash chain — włączyć w wersji > MVP
  hash_chain: false

metrics:
  enabled: true
  # Gdzie zapisać plik .prom dla Node Exporter textfile collector
  textfile_directory: /var/lib/node_exporter/textfile_collector
  prefix: patchpilot_
```

### Walidacja schematu (Pydantic)

```
InventoryModel
├── metadata: InventoryMetadata
├── connection: ConnectionSettings
├── hosts: list[HostModel]
│   └── HostModel: name, address, role, ssh_user?, ssh_key_path?, sudo?, tags?
├── strategy: StrategyModel
│   └── CanaryStrategy / BatchStrategy / SingleStrategy
├── health_checks: HealthCheckConfig
│   └── global: list[HealthCheckModel]
│   └── per_role: dict[role, list[HealthCheckModel]]
├── snapshot: SnapshotConfig
├── maintenance: MaintenanceConfig
├── audit: AuditConfig
└── metrics: MetricsConfig
```

Walidacja przy starcie:
- `name` musi być unikalny w bazie.
- `address` musi być IP lub FQDN.
- `ssh_key_path` musi istnieć i mieć uprawnienia 0600.
- `parallel_limit` ≤ liczby hostów.
- `strategy` musi mieć sensowne parametry (np. `canary.count` < liczba hostów z `tag_filter`).
- `health_checks` per_role muszą odnosić się do istniejących ról w `hosts`.
- `maintenance.windows` — start < end, dni muszą być z zakresu `monday..sunday`.

---

## 5. CLI — komendy, argumenty, output

### Drzewo komend

```
patchpilot
├── plan <environment> [--output json|table|yaml]
│   └── Generuje plan bez wprowadzania zmian.
├── deploy <environment>
│   ├── --strategy canary|batch|single (override inventory)
│   ├── --auto-approve (pomija potwierdzenie)
│   ├── --dry-run (tylko pokazuje co by zrobił, nie zapisuje do DB)
│   ├── --limit <pattern> (np. --limit api* — tylko matching hosty)
│   ├── --skip-health-checks (niebezpieczne — dla awaryjnych fixów)
│   ├── --force (ignoruje maintenance window i snapshot unavailable)
│   └── --resume <rollout-id> (wznowienie przerwanego rolloutu)
├── status <rollout-id> [--watch]
│   └── Wyświetla aktualny stan hostów w rolloucie.
├── rollback <rollout-id> [--host <name>] [--all]
│   └── Ręczny rollback wybranych hostów (jeśli snapshot istnieje).
├── history [--environment <env>] [--limit 20]
│   └── Lista zakończonych rolloutów.
├── audit <rollout-id> [--format json|csv]
│   └── Szczegółowy audyt jednego rolloutu.
├── validate <inventory-path>
│   └── Sprawdza składnię i semantykę pliku YAML.
└── config
    ├── init <environment>  # Tworzy szablon inventory
    └── show <environment>  # Wyświetla sparsowaną konfigurację
```

### Przykładowe outputy

#### `patchpilot plan production`

```
Rollout Plan: production
Generated: 2026-07-29 14:32:00 CEST

Hosts: 4
  api-01     (10.10.0.11)  Ubuntu 24.04  role=api
  api-02     (10.10.0.12)  Ubuntu 24.04  role=api
  api-03     (10.10.0.13)  Ubuntu 24.04  role=api
  db-01      (10.10.0.20)  Ubuntu 24.04  role=database

Strategy: canary (canary=1, batch_size=2)
Execution order:
  1. api-01 — CANARY
  2. api-02, api-03 — BATCH 1
  3. db-01  — BATCH 2 (database role — last)

Packages requiring updates: 27 (security: 8)
  linux-image-generic      6.8.0-35 → 6.8.0-40   [security]
  openssl                  3.0.13  → 3.0.14     [security]
  nginx                    1.24.0  → 1.26.1
  postgresql-16            16.3    → 16.4

Reboot required after update:
  api-01, api-02, api-03
  db-01

Snapshot availability:
  api-01     — Btrfs  ✓
  api-02     — Btrfs  ✓
  api-03     — LVM    ✓
  db-01      — unavailable (ext4, no LVM) ⚠

Health checks configured:
  api-*      — systemd:nginx.service, http://localhost:8080/health
  db-01      — tcp:5432, command:pg_isready

Estimated duration: ~12 min (plus reboots)
Dry-run: no changes will be made.
```

#### `patchpilot deploy production --strategy canary`

```
[14:32:01] Rollout 550e8400-e29b-41d4-a716-446655440000 started by piotr
[14:32:01] Maintenance window: SAT 23:00 – SUN 02:00 CEST ✓
[14:32:02] Lock acquired for environment 'production'

--- Canary Phase (1 host) ---
[14:32:03] [api-01] Starting...
[14:32:04] [api-01] [SNAPSHOT] Btrfs snapshot 'patchpilot-20260729-143204' created
[14:32:45] [api-01] [UPDATE] 27 packages upgraded (8 security)
[14:32:46] [api-01] [REBOOT] Reboot required. Rebooting...
[14:33:15] [api-01] [REBOOT] SSH restored after 29s
[14:33:16] [api-01] [HEALTH] systemd:nginx.service → PASS
[14:33:17] [api-01] [HEALTH] http://localhost:8080/health → 200 PASS
[14:33:18] [api-01] [HEALTH] journal:nginx.service → PASS
[14:33:18] [api-01] → HEALTHY ✓
[14:33:18] Canary successful. Proceeding with remaining hosts.

--- Batch 1 (2 hosts) ---
[14:33:20] [api-02] Starting...
[14:33:21] [api-02] [SNAPSHOT] Btrfs snapshot created
[14:34:02] [api-02] [UPDATE] 27 packages upgraded
[14:34:03] [api-02] [REBOOT] Rebooting...
[14:34:32] [api-02] [REBOOT] SSH restored
[14:34:33] [api-02] [HEALTH] systemd:nginx.service → PASS
[14:34:34] [api-02] [HEALTH] http://localhost:8080/health → 200 PASS
[14:34:34] [api-02] → HEALTHY ✓

[14:33:20] [api-03] Starting...
[14:33:22] [api-03] [SNAPSHOT] LVM snapshot created
[14:34:05] [api-03] [UPDATE] 27 packages upgraded
[14:34:06] [api-03] [REBOOT] Rebooting...
[14:34:35] [api-03] [REBOOT] SSH restored
[14:34:36] [api-03] [HEALTH] systemd:nginx.service → PASS
[14:34:37] [api-03] [HEALTH] http://localhost:8080/health → 200 PASS
[14:34:37] [api-03] → HEALTHY ✓

--- Batch 2 (1 host) ---
[14:34:40] [db-01] Starting...
[14:34:41] [db-01] [SNAPSHOT] unavailable (warn) — continuing without snapshot
[14:35:20] [db-01] [UPDATE] 12 packages upgraded
[14:35:21] [db-01] [REBOOT] Rebooting...
[14:35:55] [db-01] [REBOOT] SSH restored
[14:35:56] [db-01] [HEALTH] tcp:5432 → PASS
[14:35:57] [db-01] [HEALTH] command:pg_isready → PASS
[14:35:57] [db-01] → HEALTHY ✓

[14:35:58] Rollout completed.
  Hosts: 4 healthy, 0 failed, 0 rolled back
  Duration: 3m 57s
  Metrics written to /var/lib/node_exporter/textfile_collector/patchpilot.prom
```

#### Scenariusz awarii (health check fail)

```
[14:34:40] [api-03] Starting...
[14:34:42] [api-03] [SNAPSHOT] Btrfs snapshot created
[14:35:23] [api-03] [UPDATE] 27 packages upgraded
[14:35:24] [api-03] [REBOOT] Rebooting...
[14:35:53] [api-03] [REBOOT] SSH restored
[14:35:54] [api-03] [HEALTH] systemd:nginx.service → PASS
[14:35:55] [api-03] [HEALTH] http://localhost:8080/health → 503 FAIL
[14:35:55] [api-03] [HEALTH] journal:nginx.service → "upstream prematurely closed" FAIL
[14:35:55] [api-03] → FAILED ✗
[14:35:56] Rollout STOPPED. Remaining hosts (db-01) will NOT be updated.
[14:35:57] [api-03] [ROLLBACK] Restoring Btrfs snapshot 'patchpilot-20260729-143442'...
[14:36:10] [api-03] [ROLLBACK] Snapshot restored. Rebooting...
[14:36:40] [api-03] [ROLLBACK] SSH restored after reboot
[14:36:41] [api-03] [ROLLBACK] systemd:nginx.service → PASS
[14:36:42] [api-03] [ROLLBACK] http://localhost:8080/health → 200 PASS
[14:36:42] [api-03] → ROLLED_BACK ✓

[14:36:43] Rollout finished with failures.
  Hosts: 2 healthy, 1 rolled_back, 1 skipped (db-01)
  Duration: 4m 42s
  Run: patchpilot rollback 550e8400-e29b-41d4-a716-446655440000 --host db-01
      to update remaining hosts after fixing the issue.
```

---

## 6. Inventory — model danych i walidacja

### Model Pydantic (pseudokod)

```python
class HostModel(BaseModel):
    name: str                    # unikalny w env
    address: IPv4Address | IPv6Address | constr(regex=FQDN)
    role: str
    ssh_user: str | None
    ssh_key_path: Path | None
    ssh_port: int = 22
    sudo: bool = True
    sudo_password: SecretStr | None = None
    tags: list[str] = []

class ConnectionSettings(BaseModel):
    ssh_user: str = "root"
    ssh_key_path: Path
    ssh_timeout: int = 30
    sudo: bool = False
    parallel_limit: int = Field(default=5, ge=1, le=100)
    retry: RetryConfig

class StrategyModel(BaseModel):
    type: Literal["canary", "batch", "single"]
    canary: CanaryConfig | None
    batch: BatchConfig | None
    # single nie wymaga dodatkowej konfiguracji

class InventoryModel(BaseModel):
    metadata: InventoryMetadata
    connection: ConnectionSettings
    hosts: list[HostModel]
    strategy: StrategyModel
    health_checks: HealthCheckConfig
    snapshot: SnapshotConfig
    maintenance: MaintenanceConfig
    audit: AuditConfig
    metrics: MetricsConfig
```

### Logika ładowania
1. Załaduj YAML.
2. Zastosuj `EnvVarResolver` (np. `${SSH_KEY_PATH}` → wartość ze zmiennej środowiskowej).
3. Zastosuj `!include` dla podziału konfiguracji (opcjonalnie, np. `!include roles/database.yaml`).
4. Walidacja Pydantic — jeśli błąd, wyjdź z czytelnym komunikatem.
5. Deduplikacja `host.name`.
6. Sprawdzenie czy wszystkie role w `health_checks.per_role` istnieją w `hosts`.
7. Sprawdzenie czy `connection.ssh_key_path` istnieje i `stat().st_mode == 0o100600`.
8. Sprawdzenie czy `strategy.canary.tag_filter` matchuje przynajmniej 1 host.

---

## 7. SSH Engine — architektura połączeń

### Cel
Jednoczesne, asynchroniczne połączenie do wielu hostów z pulą połączeń, retry, timeoutami i obsługą sudo.

### Kluczowe komponenty

```python
class SSHConnectionPool:
    """Zarządza pulą asynchronicznych połączeń SSH."""
    # - Semaphore(limit=parallel_limit)
    # - Dict[host_name, SSHClientConnection]
    # - Health check połączeń (keepalive)

class SSHSession:
    """Wrapper na asyncssh.SSHClientConnection dla jednego hosta."""
    # - run(command, sudo=False, timeout=30) -> SSHResult
    # - run_stream(command) -> AsyncIterator[str] (dla długich operacji jak apt upgrade)
    # - upload_file(local, remote)
    # - download_file(remote, local)

class SSHResult:
    return_code: int
    stdout: str
    stderr: str
    duration_ms: int
```

### Retry i backoff

```
RetryPolicy:
  max_attempts: 3
  backoff: exponential (1s, 2s, 4s) lub stały
  retry_on:
    - ConnectionError (SSH nieosiągalny)
    - TimeoutError
    - asyncio.TimeoutError
  no_retry_on:
    - AuthenticationError (zły klucz — bez sensu retry)
    - PermissionError (sudo bez uprawnień)
```

### Sudo
- Jeśli `sudo=True` i `sudo_password=None` → zakładamy NOPASSWD. Komenda jest prefiksowana `sudo --non-interactive ...`.
- Jeśli `sudo_password` jest ustawione → wysyłamy `echo {pass} | sudo -S ...`. **Ostrzeżenie**: hasło jest przekazywane w linii komendy, więc widoczne w `ps`. Preferowane jest NOPASSWD w sudoers.
- Wszystkie komendy sudo powinny używać `sudo --non-interactive` aby nie zawisnąć na prompt.

### Timeouty
- **Connect timeout**: 30s (TCP + handshake SSH).
- **Command timeout**: per-komenda, np. snapshot 120s, apt upgrade 600s, health check 10s.
- **Session idle timeout**: 60s — jeśli brak aktywności, zamknij połączenie (oszczędność zasobów).

### Równoległość
- Semaphore na poziomie `SSHConnectionPool` (max N jednoczesnych sesji).
- Semaphore na poziomie hosta (max 1 jednoczesna operacja per host w danym rolloutu — zapobiega race conditions).
- Użycie `asyncio.gather(*coros, return_exceptions=True)` aby zebrać wyniki nawet przy częściowych błędach.

### Bezpieczeństwo
- Klucz SSH dla PatchPilota powinien być dedykowany, bez passphrase (lub z ssh-agent).
- Wymuszamy `HashKnownHosts yes` i `StrictHostKeyChecking` (konfigurowalne: `ask` vs `accept-new` vs `yes`).
- Nie logujemy haseł ani kluczy prywatnych.
- SSH commands są budowane przez listę (a nie string format) aby uniknąć injection, ale finalnie i tak przekazywane jako string do SSH — trzeba uważać na `;`, `|`, `$()` w inputach. Wszystkie dane pochodzące z zewnątrz (nazwy hostów, pakiety) muszą być escapowane (`shlex.quote`).

---

## 8. Rollout Engine — serce systemu

### 8.1 Planner (`planner.py`)

Planner nie wprowadza zmian. Zbiera informacje.

```python
class RolloutPlan:
    environment: str
    strategy: Strategy
    hosts: list[PlannedHost]
    total_packages: int
    security_packages: int
    reboot_required_count: int
    estimated_duration_min: int
    warnings: list[str]

class PlannedHost:
    host: HostModel
    distro: DistroInfo           # z /etc/os-release
    package_manager: str         # "apt", "dnf", "pacman"
    available_updates: list[PackageUpdate]
    reboot_required: bool
    snapshot_technology: str | None   # "btrfs", "lvm", "zfs", None
    snapshot_available: bool
    health_checks: list[HealthCheckModel]
    execution_group: int         # batch / canary number
```

#### Algorytm planowania
1. **Pobierz dystrybucję** — `cat /etc/os-release` przez SSH. Parsuj `ID`, `VERSION_ID`.
2. **Wykryj package manager** — mapowanie: `ubuntu|debian` → `apt`, `fedora|rhel|rocky|almalinux` → `dnf`, `arch` → `pacman`.
3. **Dry-run aktualizacji**:
   - apt: `apt-get --dry-run upgrade` (parsuj linie `Inst ...`).
   - dnf: `dnf check-update` (exit 100 = updates available).
   - pacman: `pacman -Qu`.
4. **Detekcja security updates**:
   - apt: `apt-get upgrade --dry-run` + `grep -i security` (lub `apt-get dist-upgrade` z `--show-progress`). Można też użyć `apt-get changelog` lub narzędzi systemowych (`unattended-upgrade --dry-run`).
   - dnf: `dnf updateinfo list security`.
   - pacman: brak natywnego — polegamy na Arch Security Tracker (opcjonalnie).
5. **Detekcja rebootu**:
   - apt: sprawdź `/var/run/reboot-required` (tworzony przez `update-notifier-common`).
   - dnf: `needs-restarting -r` (z `dnf-utils` / `yum-utils`).
   - pacman: sprawdź czy updated kernel > running kernel (`uname -r` vs `/boot` lub `pacman -Q linux`).
6. **Wykrycie snapshotów**:
   - `findmnt -n -o FSTYPE /` → jeśli `btrfs`, sprawdź czy subvolume można snapshotować.
   - `lsblk -o NAME,TYPE,MOUNTPOINT` + `lvs` → LVM?
   - `zfs list` → ZFS?
   - Jeśli snapshot nie jest dostępny i `snapshot.on_unavailable == abort`, plan zawiera błąd krytyczny.
7. **Przypisanie do grup wykonania** — Strategy przypisuje `execution_group`.
   - Canary: grupa 0 = kanarek, grupa 1..N = reszta.
   - Batch: grupy 0,1,2...
   - Single: każdy host w osobnej grupie.

### 8.2 Strategie (`strategies.py`)

```python
from abc import ABC, abstractmethod

class RolloutStrategy(ABC):
    @abstractmethod
    def group_hosts(self, hosts: list[HostModel]) -> list[list[HostModel]]:
        """Zwraca listę batchy (list hostów)."""

class CanaryStrategy(RolloutStrategy):
    def __init__(self, canary_count: int, tag_filter: str | None):
        ...
    # Logika:
    # 1. Wybierz kanarki: hosty matchujące tag_filter (losowo lub po nazwie).
    # 2. Reszta hostów posortowana: najpierw non-database, na końcu database.
    # 3. Batchuj resztę wg batch_size.

class BatchStrategy(RolloutStrategy):
    def __init__(self, batch_size: int, wait_for_batch: bool):
        ...
    # Logika:
    # 1. Sortowanie wg priorytetu roli (configurowalne w YAML, np. worker < api < database).
    # 2. Dzielenie na grupy po batch_size.

class SingleStrategy(RolloutStrategy):
    # Jeden host na batch.
```

**Decyzja projektowa**: Strategia nie decyduje o rollbacku — to robi Executor na podstawie health checków. Strategia tylko decyduje o kolejności i rozmiarze batchy.

### 8.3 Executor (`executor.py`)

Executor to **async event loop** iterujący po batchach.

```python
class RolloutExecutor:
    async def execute(self, rollout: Rollout, plan: RolloutPlan):
        for batch in plan.batches:
            # Czekaj na maintenance window (jeśli wymagane)
            self._assert_in_maintenance_window()

            # Wykonaj wszystkie hosty w batchu równolegle (z limitem)
            results = await asyncio.gather(
                *[self._execute_host(rollout, host) for host in batch],
                return_exceptions=True
            )

            # Sprawdź wyniki
            if any(r.status == HostStatus.FAILED for r in results):
                # Zatrzymaj rollout. Rollback zepsutych hostów w tym batchu.
                await self._rollback_failed_hosts(rollout, results)
                await self._finalize_rollout(rollout, stopped=True)
                return

            # Jeśli strategy.wait_for_batch — czekamy na wszystkie health checki
            # zanim przejdziemy do następnego batcha (już wykonane w _execute_host).

        await self._finalize_rollout(rollout, stopped=False)
```

#### `_execute_host` — krok po kroku

```
1. Zapisz w DB: rollout_steps (type='begin', status='in_progress')
2. SNAPSHOT (jeśli available):
   a. Wywołaj SnapshotProvider.create()
   b. Zapisz w DB: snapshot_type, snapshot_name, snapshot_created_at
   c. Jeśli timeout → FAIL (lub WARN + continue, wg configu)
3. UPDATE PACKAGES:
   a. PackageManager.apply_updates(dry_run=False)
   b. Zapisz listę zainstalowanych pakietów w DB
   c. Jeśli błąd apt/dnf → FAIL (to nie jest health check fail, to fail aktualizacji)
4. REBOOT (jeśli required):
   a. Wyślij `systemctl reboot` lub `reboot`
   b. Zamknij sesję SSH
   c. Czekaj na powrót SSH (poll co 5s, max 300s)
   d. Jeśli nie wróci → FAIL (host może być martwy)
5. HEALTH CHECKS:
   a. Dla każdego checku w kolejności:
      - Uruchom na hoście (lub zdalnie dla HTTP/TCP)
      - Zapisz wynik w health_check_results
   b. Agregacja: ALL(check.passed) ? HEALTHY : FAIL
6. Zapisz finalny stan hosta w rollout_hosts
```

### 8.4 State Machine (`state_machine.py`)

Każdy host w rolloucie ma swój stan. Stan jest źródłem prawdy dla recovery.

#### Stany i tranzycje

```
                    ┌─────────────┐
                    │   PENDING   │
                    └──────┬──────┘
                           │ begin
              ┌────────────▼────────────┐
              │      SNAPSHOTTING       │
              │  (opcjonalnie, jeśli    │
              │   snapshot available)   │
              └──────┬────────┬─────────┘
                     │        │ snapshot ok / skipped
        snapshot fail│        ▼
                     │   ┌──────────┐
                     │   │ UPDATING │
                     │   └────┬─────┘
                     │        │ update ok
                     │        ▼
                     │   ┌──────────┐
                     │   │ REBOOTING│  ← opcjonalnie
                     │   └────┬─────┘
                     │        │ ssh restored
                     │        ▼
                     │   ┌──────────┐
                     │   │ VERIFYING│ (health checks)
                     │   └────┬─────┘
                     │        │ all pass
                     │        ▼
                     │   ┌──────────┐
                     └──►│  FAILED  │
                         └────┬─────┘
                              │ start rollback
                              ▼
                         ┌──────────┐
                         │ROLLING_  │
                         │ BACK     │
                         └────┬─────┘
                              │ rollback ok
                              ▼
                         ┌──────────┐
                         │ROLLED_   │
                         │ BACK     │
                         └──────────┘
```

**Zasady:**
- Tranzycja może nastąpić tylko w górę diagramu (nie można wrócić do `UPDATING` z `VERIFYING`).
- Jeśli program crashuje między `UPDATING` a `REBOOTING`, przy recovery musimy sprawdzić czy host wymaga rebootu (np. `/var/run/reboot-required` istnieje) i wykonać go, zanim przejdziemy do `VERIFYING`.
- Stany są atomowo aktualizowane w DB w ramach tej samej transakcji co `rollout_steps`.

---

## 9. Package Managers — abstrakcja i implementacje

### Interfejs abstrakcyjny

```python
from dataclasses import dataclass
from abc import ABC, abstractmethod

@dataclass
class PackageUpdate:
    name: str
    current_version: str
    new_version: str
    source: str           # repozytorium
    is_security: bool
    size_bytes: int | None

@dataclass
class UpdateResult:
    success: bool
    updated_packages: list[PackageUpdate]
    failed_packages: list[str]
    stdout: str
    stderr: str
    reboot_required: bool
    duration_seconds: float

class PackageManager(ABC):
    @abstractmethod
    async def check_updates(self) -> list[PackageUpdate]: ...

    @abstractmethod
    async def apply_updates(self, dry_run: bool = False) -> UpdateResult: ...

    @abstractmethod
    async def requires_reboot(self) -> bool: ...

    @classmethod
    @abstractmethod
    def detect(cls, distro_id: str, conn: SSHSession) -> bool: ...
```

### AptPackageManager (`apt.py`)

**Szczegóły implementacyjne:**
- apt jest **interaktywny** domyślnie. Musimy wymusić `DEBIAN_FRONTEND=noninteractive`.
- `apt-get` (CLI) vs `apt` (bardziej interaktywny, progress bar). Używamy `apt-get` dla determinizmu.
- **Check updates**:
  ```bash
  apt-get update -qq && apt-get --just-print upgrade
  ```
  Parsujemy linie `Inst <pkg> [<old>] (<new> ...)`.
- **Security detection**:
  ```bash
  apt-get --just-print dist-upgrade | grep -i security
  ```
  Albo lepiej: `unattended-upgrade --dry-run` (jeśli zainstalowany) — daje precyzyjną listę security patches.
- **Apply updates**:
  ```bash
  DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::="--force-confold" upgrade
  ```
  `-o Dpkg::Options::="--force-confold"` — zachowaj stare configi, nie pytaj.
- **Reboot detection**:
  ```bash
  test -f /var/run/reboot-required
  ```
  Lub sprawdź czy aktualizowany był kernel (`linux-image-*` lub `generic`) i czy wersja w `/boot` > `uname -r`.

### DnfPackageManager (`dnf.py`)

**Szczegóły:**
- `dnf check-update` — zwraca exit 100 jeśli są aktualizacje, 0 jeśli nie ma.
- **Security updates**: `dnf updateinfo list security` (wymaga zainstalowanej bazy danych updateinfo, domyślnie w RHEL/Rocky).
- **Apply**:
  ```bash
  dnf -y upgrade
  ```
  Można ograniczyć do security: `dnf -y upgrade --security` (jeśli użytkownik tego chce — flaga w configu).
- **Reboot detection**:
  ```bash
  needs-restarting -r
  ```
  (z pakietu `dnf-utils` lub `yum-utils`).

### PacmanPackageManager (`pacman.py`) — wersja 2+

**Szczegóły:**
- `pacman -Qu` — lista do zaktualizowania.
- `pacman -Syu --noconfirm` — aktualizacja.
- Brak natywnego wsparcia dla security flags. `is_security` zawsze `False` w MVP, w v2 można zintegrować Arch Security Tracker API.
- Reboot: porównanie `uname -r` z najnowszym kernelem w `/boot` lub `pacman -Q linux`.

---

## 10. Snapshots i Rollback

### 10.1 Detector (`snapshots/detector.py`)

```python
class SnapshotDetector:
    async def detect(self, conn: SSHSession) -> SnapshotProvider | None:
        """Wykrywa najlepszy dostępny system snapshotów na hoście."""
```

**Kolejność priorytetów:**
1. **Btrfs** — jeśli `findmnt -n -o FSTYPE / == btrfs` i `btrfs subvolume list /` działa.
2. **ZFS** — jeśli `zfs list` zwraca pool.
3. **LVM** — jeśli root jest na LV (`lvdisplay` + `lv_name`).
4. **Timeshift** — jeśli zainstalowany, używany jako wrapper na Btrfs/Rsync.
5. **VM snapshot** — opcjonalnie, przez API (AWS EC2, Proxmox, VMware).

### 10.2 SnapshotProvider interfejs

```python
class SnapshotProvider(ABC):
    @abstractmethod
    async def create(self, conn: SSHSession, label: str) -> SnapshotInfo: ...

    @abstractmethod
    async def restore(self, conn: SSHSession, snapshot: SnapshotInfo) -> bool: ...

    @abstractmethod
    async def delete(self, conn: SSHSession, snapshot: SnapshotInfo) -> bool: ...
```

### 10.3 Implementacje

#### BtrfsSnapshotProvider (`snapshots/btrfs.py`)
- **Create**:
  ```bash
  btrfs subvolume snapshot / /path/to/snapshots/patchpilot-{label}
  ```
  Wymaga wolnej przestrzeni w subvolume. Snapshot jest natychmiastowy (copy-on-write).
- **Restore**:
  ```bash
  # Boot z innego medium / recovery, lub:
  btrfs subvolume delete /
  btrfs subvolume snapshot /path/to/snapshots/patchpilot-{label} /
  ```
  **Uwaga**: restore na żywym systemie jest niemożliwy (root jest zamontowany). W praktyce:
  - Jeśli host jest VM: snapshot VM jest lepszy.
  - Jeśli Btrfs na root: przywrócenie wymaga rebootu do snapshotu przez bootloader (grub-btrfs) lub bootowania z recovery ISO. **To jest problem.**
  - **Rozwiązanie**: Zamiast restore Btrfs online, możemy użyć `btrfs replace` lub zrobić `rsync` z snapshotu. Ale to skomplikowane.
  - **Decyzja MVP**: Jeśli Btrfs — tworzymy snapshot jako **backup**, ale rollback może wymagać ręcznej interwencji lub rebootu do snapshotu (jeśli grub-btrfs jest skonfigurowany). W pełni automatyczny rollback działa najlepiej z LVM (online merge) lub ZFS.

#### LvmSnapshotProvider (`snapshots/lvm.py`)
- **Create**:
  ```bash
  lvcreate -L {size} -s -n patchpilot-{label} /dev/mapper/vg-root
  ```
  Wymaga wolnego miejsca w VG. Rozmiar snapshotu: np. 20% rozmiaru LV lub konfigurowalne.
- **Restore** (online!):
  ```bash
  lvconvert --merge /dev/mapper/vg-patchpilot-{label}
  # następnie reboot
  ```
  Merge jest wykonywany przy następnym uaktywnieniu LV (czyli reboot). **To oznacza, że rollback LVM wymaga rebootu.**
- **Decyzja**: Rollback = `lvconvert --merge` + `reboot`. Po reboot system wraca do stanu sprzed snapshotu. Jest to **atomowe** i bezpieczne.

#### ZfsSnapshotProvider (`snapshots/zfs.py`)
- **Create**:
  ```bash
  zfs snapshot tank/root@patchpilot-{label}
  ```
- **Restore** (online!):
  ```bash
  zfs rollback tank/root@patchpilot-{label}
  ```
  ZFS pozwala na **natychmiastowy rollback** bez rebootu (jeśli dataset nie jest aktywnie modyfikowany przez inny proces). Dla root datasetu może to być ryzykowne (otwarte pliki), ale w praktyce działa jeśli usługi są zatrzymane.
  - **Bezpieczniej**: zatrzymać kluczowe usługi przed rollbackiem, wykonać `zfs rollback`, uruchomić usługi ponownie.

### 10.4 Flow rollbacku (Executor)

```
IF health_check FAILED:
  1. Oznacz hosta w DB jako FAILED.
  2. IF snapshot exists:
     a. IF provider == LVM:
        - lvconvert --merge <snapshot>
        - reboot
        - czekaj na SSH
     b. IF provider == ZFS:
        - zfs rollback <snapshot>
        - (opcjonalnie restart usług)
        - health check ponownie
     c. IF provider == Btrfs:
        - grub-btrfs-reboot /path/to/snapshot (jeśli dostępne)
        - reboot
        - czekaj na SSH
     d. IF provider == none:
        - Oznacz jako FAILED, rollback niemożliwy.
  3. Oznacz jako ROLLED_BACK (jeśli snapshot się udał) lub FAILED_NO_ROLLBACK.
  4. Zatrzymaj dalsze hosty (przerwij rollout).
```

---

## 11. Health Checks — szczegóły

### 11.1 Konfiguracja per-rola

Health checki są **łączone**: globalne + per-rola. Dla roli `api` wszystkie trzy listy są sumowane.

### 11.2 Typy checków i implementacja

#### SystemdCheck (`health/systemd.py`)
```bash
systemctl is-active {service}  # 0 = active
systemctl is-failed {service}   # 0 = failed
```
- Parametry: `service`, `state: active|running`.
- Timeout: 5s.

#### HttpCheck (`health/http.py`)
- Wymaga `curl` lub `python -m http.client` na hoście (lub wykonania z maszyny sterującej).
- **Decyzja**: Domyślnie uruchamiamy z maszyny sterującej (PatchPilot sprawdza endpoint). Dlaczego? Bo usługa może nasłuchiwać tylko na loopback i być niedostępna z zewnątrz. Ale jeśli host jest za NAT/VPN, możemy wykonać curl przez SSH.
- Parametry: `url`, `method`, `expected_status`, `timeout`, `retries`, `expected_body_regex`.
- Retry: przy `503` czy `502` możemy poczekać 5s i retry (usługa może się dopiero podnieść).

#### TcpCheck (`health/tcp.py`)
- Implementacja przez SSH: `nc -z {host} {port}` lub `timeout {timeout} bash -c 'cat < /dev/tcp/{host}/{port}'`.
- Lub z maszyny sterującej przez `asyncio.open_connection`.

#### CommandCheck (`health/command.py`)
- Wykonanie dowolnej komendy przez SSH.
- Parametry: `command`, `expected_exit_code` (domyślnie 0), `timeout`.
- **Bezpieczeństwo**: command jest brany z configu YAML (zaufanego źródła), nie z user input. Escapujemy przez `shlex.quote`.

#### JournalCheck (`health/journal.py`)
- Wykonanie przez SSH:
  ```bash
  journalctl -u {service} --since "{lookback_seconds} seconds ago" --no-pager
  ```
- Następnie grep na `forbidden_patterns`.
- Jeśli którykolwiek pattern wystąpił → FAIL.
- Parametry: `service`, `forbidden_patterns: list[str]`, `lookback_seconds`.

### 11.3 Agregacja wyników

```python
class HealthCheckSuite:
    async def run_all(self, conn: SSHSession) -> HealthSummary:
        # Uruchamia checki w kolejności (nie równolegle, bo mogą wpływać na siebie)
        # Jeśli którykolwiek FAIL → zatrzymaj dalsze checki i zwróć FAIL
        # Ale zapisz wszystkie wyniki (nawet te nieuruchomione?) — nie, zapisujemy tylko wykonane.
```

**Decyzja**: Health checki są uruchamiane **szeregowo** na danym hoście (max 1 check na raz) aby nie obciążać usługi. Między hostami mogą być równolegle.

---

## 12. Maintenance Windows

### Logika

```python
class MaintenanceWindow:
    timezone: ZoneInfo
    start: time          # np. time(23, 0)
    end: time            # np. time(2, 0)
    days: set[Weekday]

    def is_open(self, dt: datetime) -> bool:
        # Konwertuj dt do timezone okna
        local_dt = dt.astimezone(self.timezone)
        # Sprawdź dzień tygodnia
        if local_dt.weekday() not in self.days:
            return False
        # Sprawdź czy czas w przedziale (uwzględnij przekroczenie północy)
        t = local_dt.time()
        if self.start <= self.end:
            return self.start <= t <= self.end
        else:
            return t >= self.start or t <= self.end
```

### Kiedy sprawdzać?
- **Planowanie** (`patchpilot plan`): może odbywać się zawsze (jeśli `plan_outside_window: true`).
- **Deploy** (`patchpilot deploy`): przed każdym batchem sprawdź `is_open(now)`. Jeśli okno się zamknie w trakcie batcha — dokończ aktualny batch, ale nie zaczynaj nowego. Jeśli `deploy_outside_window: false` — odmów startu.
- **Resume**: ponownie sprawdź okno.

---

## 13. Locking & Recovery

### 13.1 Blokady (Locking)

**Cel**: Nie można uruchomić dwóch rolloutów na tym samym `environment` jednocześnie.

**Implementacja (SQLite)**:
- Używamy tabeli `rollout_locks`:
  ```sql
  CREATE TABLE rollout_locks (
      environment TEXT PRIMARY KEY,
      rollout_id TEXT NOT NULL,
      acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );
  ```
- Przy starcie deploy: `INSERT INTO rollout_locks (environment, rollout_id) VALUES (?, ?)`.
- Jeśli `IntegrityError` (PRIMARY KEY conflict) → blokada zajęta, odmowa.
- Przy zakończeniu (sukces, fail, abort): `DELETE FROM rollout_locks WHERE environment = ?`.
- **Stare blokady**: Jeśli blokada istnieje > 24h, uznajemy ją za martwą i nadpisujemy (z ostrzeżeniem).

### 13.2 Idempotencja kroków

Tabela `rollout_steps` zapamiętuje co zostało zrobione:
```sql
CREATE TABLE rollout_steps (
    id INTEGER PRIMARY KEY,
    rollout_host_id INTEGER NOT NULL,
    step_type TEXT NOT NULL,   -- snapshot, update, reboot, verify, rollback
    status TEXT NOT NULL,      -- pending, in_progress, completed, failed
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    log_output TEXT,
    UNIQUE(rollout_host_id, step_type)   -- jeden krok danego typu per host
);
```

Przy wznawianiu:
- Jeśli `status == completed` → pomiń.
- Jeśli `status == in_progress` → uznaj za niepewny, wykonaj ponownie (lub sprawdź stan na hoście).
- Jeśli `status == failed` → zatrzymaj (wymagana decyzja operatora).

### 13.3 Recovery procedura

Przy starcie każdego deploy / resume:
1. Sprawdź czy w `rollouts` istnieją rekordy ze statusem `in_progress` (i nie są starsze niż X godzin).
2. Jeśli tak:
   - Wyświetl listę "wiszących" rolloutów.
   - Opcje operatora:
     - `resume` — kontynuuj od ostatniego stanu (jeśli host był w `REBOOTING`, sprawdź SSH).
     - `rollback` — wycofaj wszystkie hosty z tego rolloutu (jeśli snapshoty istnieją).
     - `abort` — oznacz jako `aborted` (ręczna interwencja na hoście).
3. Jeśli operator nie poda `--resume <id>`, domyślnie pytamy interaktywnie (lub failujemy w CI).

### 13.4 Crash recovery — scenariusze

| Moment crashu | Co jest zapisane w DB | Jak recovery |
|---------------|----------------------|--------------|
| Po snapshot, przed update | `snapshot` = completed | Sprawdź czy snapshot istnieje na hoście. Jeśli tak, idź do update. Jeśli nie, utwórz ponownie. |
| Po update, przed reboot | `update` = completed, `reboot` = pending | Sprawdź czy `/var/run/reboot-required`. Jeśli tak — reboot. Jeśli nie — przejdź do health check. |
| Po reboot, przed health | `reboot` = completed (SSH wróciło) | Wykonaj health checks. |
| W trakcie health check | `verify` = in_progress | Powtórz wszystkie health checki (szybkie, bezpieczne). |
| W trakcie rollback | `rollback` = in_progress | Sprawdź czy snapshot nadal istnieje i czy system działa. Jeśli nie — kontynuuj rollback. |

---

## 14. Model danych — pełny schemat SQL

### Diagram relacji (ASCII)

```
┌─────────────────────────────────────────────────────────────────┐
│                          rollouts                                │
├─────────────────────────────────────────────────────────────────┤
│ id (UUID PK)    │ env_name │ strategy │ status │ created_by     │
│ started_at      │ finished_at │ maintenance_ok │ plan_json      │
│ final_report_json │ metrics_written │ aborted_reason             │
└────────┬────────────────────────────────────────────────────────┘
         │ 1 : N
┌────────▼─────────────────────────────────────────────────────────┐
│                       rollout_hosts                              │
├─────────────────────────────────────────────────────────────────┤
│ id (PK)         │ rollout_id (FK) │ host_name │ host_role       │
│ address         │ status │ snapshot_type │ snapshot_name        │
│ snapshot_created_at │ updated_at │ reboot_required │ error_log   │
└────────┬─────────────────────────────────────────────────────────┘
         │ 1 : N
┌────────▼─────────────────────────────────────────────────────────┐
│                       rollout_steps                              │
├─────────────────────────────────────────────────────────────────┤
│ id (PK)         │ rollout_host_id (FK) │ step_type │ status    │
│ started_at      │ finished_at │ log_output │ exit_code          │
│ packages_json   │ snapshot_info_json │ retry_count               │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    health_check_results                          │
├─────────────────────────────────────────────────────────────────┤
│ id (PK)         │ rollout_host_id (FK) │ check_type │ passed    │
│ details         │ checked_at │ duration_ms │ retry_number      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       audit_events                               │
├─────────────────────────────────────────────────────────────────┤
│ id (PK)         │ rollout_id (FK) │ event_type │ actor          │
│ timestamp       │ previous_hash │ event_hash │ payload_json     │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       rollout_locks                              │
├─────────────────────────────────────────────────────────────────┤
│ environment (PK) │ rollout_id │ acquired_at                   │
└─────────────────────────────────────────────────────────────────┘
```

### Szczegółowy DDL (SQLite)

```sql
-- Rollout główny
CREATE TABLE rollouts (
    id TEXT PRIMARY KEY,                     -- UUID v4
    env_name TEXT NOT NULL,
    strategy_type TEXT NOT NULL CHECK(strategy_type IN ('canary','batch','single')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','in_progress','paused','completed','failed','aborted')),
    created_by TEXT NOT NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    maintenance_window_ok BOOLEAN,
    plan_json TEXT,                          -- serializacja RolloutPlan (dla audytu)
    final_report_json TEXT,
    metrics_written BOOLEAN DEFAULT FALSE,
    aborted_reason TEXT
);
CREATE INDEX idx_rollouts_env_status ON rollouts(env_name, status);

-- Hosty w rolloucie
CREATE TABLE rollout_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rollout_id TEXT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
    host_name TEXT NOT NULL,
    host_role TEXT NOT NULL,
    address TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','snapshotting','updating','rebooting','verifying','healthy','failed','rolling_back','rolled_back','skipped')),
    snapshot_type TEXT CHECK(snapshot_type IN ('btrfs','lvm','zfs','timeshift','vm','none')),
    snapshot_name TEXT,
    snapshot_created_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reboot_required BOOLEAN,
    error_log TEXT,
    UNIQUE(rollout_id, host_name)
);
CREATE INDEX idx_rollout_hosts_rollout ON rollout_hosts(rollout_id);
CREATE INDEX idx_rollout_hosts_status ON rollout_hosts(status);

-- Kroki wykonane na hoście
CREATE TABLE rollout_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rollout_host_id INTEGER NOT NULL REFERENCES rollout_hosts(id) ON DELETE CASCADE,
    step_type TEXT NOT NULL
        CHECK(step_type IN ('snapshot','update','reboot','verify','rollback','begin')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','in_progress','completed','failed','skipped')),
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    log_output TEXT,
    exit_code INTEGER,
    packages_json TEXT,          -- lista PackageUpdate zainstalowanych (tylko dla step_type='update')
    snapshot_info_json TEXT,     -- metadane snapshotu
    retry_count INTEGER DEFAULT 0,
    UNIQUE(rollout_host_id, step_type)
);
CREATE INDEX idx_rollout_steps_host ON rollout_steps(rollout_host_id);

-- Wyniki health checków
CREATE TABLE health_check_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rollout_host_id INTEGER NOT NULL REFERENCES rollout_hosts(id) ON DELETE CASCADE,
    check_type TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    details TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_ms INTEGER,
    retry_number INTEGER DEFAULT 0
);
CREATE INDEX idx_health_host ON health_check_results(rollout_host_id);

-- Audyt z opcjonalnym hash chain
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rollout_id TEXT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
        CHECK(event_type IN ('rollout_started','host_snapshot','host_update','host_reboot',
                             'host_health_check','host_rollback','rollout_completed',
                             'rollout_failed','rollout_aborted','step_retry')),
    actor TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    previous_hash TEXT,          -- SHA256 poprzedniego eventu (lub null dla pierwszego)
    event_hash TEXT NOT NULL,    -- SHA256(previous_hash + payload_json)
    payload_json TEXT NOT NULL   -- szczegóły zdarzenia
);
CREATE INDEX idx_audit_rollout ON audit_events(rollout_id);
CREATE INDEX idx_audit_timestamp ON audit_events(timestamp);

-- Blokady środowisk
CREATE TABLE rollout_locks (
    environment TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL,
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 15. Audit i Hash Chain

### Audit (zawsze włączony)

Każda istotna operacja generuje `audit_events`:
- `rollout_started` — kto, kiedy, jaki plan (plan_json w payload).
- `host_snapshot` — który host, jaki typ snapshotu, nazwa.
- `host_update` — który host, ile pakietów, lista nazw.
- `host_reboot` — który host, czas oczekiwania na SSH.
- `host_health_check` — który host, który check, pass/fail, details.
- `host_rollback` — który host, powód, wynik rollbacku.
- `rollout_completed/failed/aborted` — podsumowanie.

### Hash Chain (opcjonalnie, wersja 2+)

**Mechanizm**:
- Pierwszy event w rolloutu: `previous_hash = "0" * 64`.
- `event_hash = SHA256(previous_hash + canonical_json(payload))`.
- Następny event bierze `previous_hash = event_hash` poprzedniego.

**Weryfikacja**:
```bash
patchpilot audit verify <rollout-id>
```
- Iteruje po audit_events w kolejności `id`.
- Przelicza hash na nowo.
- Jeśli którykolwiek nie zgadza się z zapisanym → wykryta manipulacja.

**Cel**: Nieufność wobec samego siebie. Jeśli ktoś zaloguje się na maszynę sterującą i zmieni wpis w SQLite "bo tak", jest to wykrywalne.

---

## 16. Metrics — Prometheus

### Format i lokalizacja

Po zakończeniu rolloutu, jeśli `metrics.enabled=True`:
```
# HELP patchpilot_last_update_timestamp Unix timestamp ostatniej aktualizacji na hoście
# TYPE patchpilot_last_update_timestamp gauge
patchpilot_last_update_timestamp{host="api-01",env="production",role="api"} 1753843200

# HELP patchpilot_update_success 1 jeśli rollout zakończony sukcesem, 0 jeśli fail/rollback
# TYPE patchpilot_update_success gauge
patchpilot_update_success{host="api-01",env="production",rollout="550e8400-..."} 1

# HELP patchpilot_packages_updated_total liczba zaktualizowanych pakietów
# TYPE patchpilot_packages_updated_total counter
patchpilot_packages_updated_total{host="api-01",env="production"} 27

# HELP patchpilot_rollback_total liczba rollbacków
# TYPE patchpilot_rollback_total counter
patchpilot_rollback_total{host="api-01",env="production",reason="health_check_fail"} 0

# HELP patchpilot_reboot_required 1 jeśli reboot był wymagany
# TYPE patchpilot_reboot_required gauge
patchpilot_reboot_required{host="api-01",env="production"} 1

# HELP patchpilot_rollout_duration_seconds czas trwania całego rolloutu
# TYPE patchpilot_rollout_duration_seconds gauge
patchpilot_rollout_duration_seconds{env="production",rollout="550e8400-..."} 237.4
```

### Gdzie zapisać?
- **Lokalnie na hoście** (zalecane przez Prometheus dla batch jobs): zapisz plik na hoście docelowym w `/var/lib/node_exporter/textfile_collector/patchpilot.prom`. Node Exporter go wystawi.
- **Jak?** Przez SSH: `echo "..." | sudo tee /var/lib/node_exporter/textfile_collector/patchpilot.prom`.
- **Na maszynie sterującej** (opcjonalnie): lokalny plik jeśli nie mamy dostępu do hostów.

**Dlaczego nie Pushgateway?**
- Prometheus rekomenduje Pushgateway tylko dla service-level metrics, nie dla batch/job-level.
- Textfile collector jest lepszy dla per-host metrics bo nie wymaga dodatkowej usługi.

---

## 17. Przepływ pracy — szczegółowo krok po kroku

### 17.1 Faza planowania (`patchpilot plan production`)

1. **Wczytanie inventory**
   - Załaduj YAML, zastosuj zmienne środowiskowe, waliduj Pydantic.
2. **Inicjalizacja SSH Pool**
   - Utwórz `SSHConnectionPool` z `parallel_limit`.
3. **Discovery (równolegle)**
   - Dla każdego hosta (semaphore):
     a. Połącz SSH.
     b. `cat /etc/os-release` → `DistroInfo`.
     c. Wybierz `PackageManager`.
     d. Wywołaj `package_manager.check_updates()`.
     e. Wywołaj `package_manager.requires_reboot()`.
     f. `SnapshotDetector.detect()`.
     g. Zamknij połączenie (lub zostaw w poolu do ponownego użycia).
4. **Agregacja**
   - Podsumuj pakiety (łącznie, security).
   - Ustal `reboot_required_count`.
5. **Strategia**
   - Wywołaj `Strategy.group_hosts()` → kolejność batchy.
6. **Generowanie raportu**
   - Sformatuj tabelę terminalową / JSON / YAML.
   - Nie zapisuj do DB (to tylko plan).

### 17.2 Faza wdrażania (`patchpilot deploy production --strategy canary`)

1. **Walidacja maintenance window**
   - `MaintenanceWindow.is_open(now)`. Jeśli nie i nie ma `--force` → błąd.
2. **Lock**
   - `INSERT INTO rollout_locks`. Jeśli istnieje → error lub `--resume`.
3. **Recovery check**
   - Sprawdź czy w DB nie ma `in_progress` rolloutów. Jeśli tak — interaktywny prompt lub wymagaj `--resume`.
4. **Utworzenie rolloutu**
   - `INSERT INTO rollouts` (UUID, env, strategy, status='in_progress', created_by=`$USER`).
   - `INSERT INTO rollout_hosts` dla każdego hosta (status='pending').
   - `INSERT INTO audit_events` (rollout_started).
5. **Planowanie (replay)**
   - Wykonaj logikę Plannera (ale tym razem tylko aby mieć pewność że nic się nie zmieniło od czasu `plan`).
6. **Executor loop**
   - Dla każdego batcha:
     a. Sprawdź maintenance window (ponownie).
     b. `asyncio.gather(*[_execute_host(h) for h in batch])`.
     c. Zbierz wyniki.
     d. Jeśli którykolwiek FAIL → zatrzymaj, rollback, `UPDATE rollouts SET status='failed'`.
     e. Jeśli wszystkie OK → przejdź do następnego batcha.
7. **Finalizacja**
   - `UPDATE rollouts SET status='completed', finished_at=...`.
   - Wygeneruj `final_report_json`.
   - Wygeneruj metryki `.prom` i wgraj na hosty (przez SSH).
   - `INSERT INTO audit_events` (rollout_completed).
   - Usuń `rollout_locks`.
8. **Raport terminalowy**
   - Podsumowanie: healthy / failed / rolled_back / skipped.

### 17.3 Faza statusu (`patchpilot status <rollout-id>`)

- `SELECT * FROM rollouts WHERE id = ?`.
- `SELECT host_name, status, snapshot_type, error_log FROM rollout_hosts WHERE rollout_id = ? ORDER BY host_name`.
- `SELECT step_type, status, started_at, finished_at FROM rollout_steps WHERE rollout_host_id IN (...)`.
- Formatowanie: tabela z kolorami (zielony=healthy, czerwony=failed, żółty=in_progress).
- `--watch`: odświeżaj co 2s (poll DB).

### 17.4 Faza rollbacku (`patchpilot rollback <rollout-id>`)

- Działa tylko na hostach ze statusem `healthy` (ręczny rollback) lub `failed` (automatyczny był już wykonany, ale można powtórzyć).
- Sprawdź czy snapshot istnieje na hoście (`snapshot_name`).
- Wywołaj `SnapshotProvider.restore()`.
- Zapisz w DB: nowy `rollout_steps` (type='rollback'), zmień `rollout_hosts.status` na `rolled_back`.

---

## 18. Testowanie — struktura i strategia

### 18.1 Unit tests (`tests/unit/`)

Testowane komponenty bez zależności zewnętrznych (mock SSH, mock DB):
- `test_inventory_parser.py` — YAML parsing, walidacja Pydantic, edge cases (brakujące pola, złe IP).
- `test_strategies.py` — czy canary wybiera właściwe hosty, czy batch dzieli poprawnie.
- `test_state_machine.py` — czy dozwolone tranzycje działają, a niedozwolone rzucają wyjątek.
- `test_planner.py` — mock `PackageManager` returning fake packages, sprawdzenie sortowania i liczenia.
- `test_health_checks.py` — mock SSH commands, sprawdzenie regexów w journal.
- `test_maintenance_window.py` — różne timezones, przekroczenie północy, DST.
- `test_audit_hash_chain.py` — weryfikacja SHA256 chain.

### 18.2 Integration tests (`tests/integration/`)

Prawdziwe kontenery Docker z systemd i SSH.

**Infrastruktura testowa**:
- `docker/ubuntu-test/Dockerfile` — Ubuntu 24.04 z `systemd`, `openssh-server`, `apt`, `sudo`, użytkownik `deploy`.
- `docker/debian-test/Dockerfile` — Debian 13.
- `docker/rocky-test/Dockerfile` — Rocky Linux 9 z `dnf`.
- `docker-compose.test.yml` — 3 kontenery + sieć wewnętrzna.

**Jak uruchomić systemd w Docker**:
- Kontenery muszą być `privileged` (dla `systemd` i montowania loop devs dla LVM).
- VOLUME `/sys/fs/cgroup`.
- Entrypoint: `/lib/systemd/systemd`.

**Fixtures (pytest)**:
- `ubuntu_container` — podniesiony kontener, gotowy do SSH.
- `ssh_session` — połączenie `asyncssh` do kontenera (klucz generowany w `setup.sh`).
- `db_session` — SQLite in-memory dla testów.

**Scenariusze integracyjne**:
- `test_apt_check_updates` — sprawdź czy `AptPackageManager.check_updates()` zwraca listę pakietów.
- `test_apt_apply_updates` — zainstaluj stary pakiet w kontenerze, wykonaj update, sprawdź czy nowy.
- `test_btrfs_snapshot` — kontener z Btrfs na loop device. Utwórz snapshot, zmień plik, przywróć snapshot, sprawdź plik.
- `test_lvm_snapshot` — kontener z LVM na loop device. Utwórz snapshot LV, merge, reboot, weryfikacja.
- `test_health_systemd` — zatrzymaj `nginx` w kontenerze, health check powinien zwrócić FAIL.
- `test_health_http` — uruchom `python -m http.server`, sprawdź czy check zwraca 200.
- `test_full_canary_success` — 3 hosty, canary=1, wszystkie przechodzą.
- `test_full_canary_fail` — 3 hosty, kanarek failuje health check, rollback, reszta skipped.

### 18.3 E2E tests (`tests/e2e/`)

Pełny scenariusz przez CLI (subprocess):
1. `patchpilot validate examples/config.lab.yaml` — musi przejść.
2. `patchpilot plan lab --output json` — parsuj JSON, sprawdź strukturę.
3. `patchpilot deploy lab --auto-approve` — uruchom, sprawdź exit code, sprawdź DB.
4. `patchpilot status <id>` — sprawdź czy wszystkie `healthy`.
5. `patchpilot history --environment lab` — sprawdź czy rollout widoczny.

---

## 19. Demo / CI (GitHub Actions)

### Scenariusz demo (do nagrania / GIF)

**Przygotowanie**:
```bash
docker compose -f docker-compose.demo.yml up -d
# 3 kontenery: ubuntu-healthy, debian-broken, rocky-pending
```

**Krok 1**: `patchpilot plan lab`
```
Hosts: 3
  ubuntu-01  — 12 updates, Btrfs snapshot available
  debian-01  — 12 updates, LVM snapshot available
  rocky-01   — 8 updates, LVM snapshot available
Strategy: canary (1 host)
```

**Krok 2**: `patchpilot deploy lab --strategy canary --auto-approve`
```
[OK] ubuntu-01 — snapshot, update, reboot, health OK
[OK] Canary successful.
[OK] debian-01 — snapshot, update, reboot...
[FAIL] debian-01 — health check http://localhost:8080/health → 503
[FAIL] Rollout STOPPED.
[OK] debian-01 — rolling back LVM snapshot...
[OK] debian-01 — reboot, health OK after rollback
[INFO] rocky-01 — skipped (rollout stopped)
```

**Krok 3**: `patchpilot status <id>`
- ubuntu-01: healthy
- debian-01: rolled_back
- rocky-01: skipped

### GitHub Actions workflow

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        distro: [ubuntu, debian, rocky]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[test]"
      - run: docker compose -f docker-compose.test.yml up -d ${{ matrix.distro }}
      - run: pytest tests/integration/ -k ${{ matrix.distro }} -v
      - run: docker compose -f docker-compose.test.yml down
```

---

## 20. Roadmapa — szczegóły

### MVP (Wersja 1) — 4-6 tygodni
**Cel**: Jedna dystrybucja, kanarek, health check, SQLite, CLI.

- [ ] Struktura projektu, `pyproject.toml`, `click` CLI scaffold.
- [ ] Inventory YAML + Pydantic walidacja.
- [ ] `SSHConnectionPool` + `asyncssh` wrapper.
- [ ] `AptPackageManager` (check, apply, reboot detection).
- [ ] `Planner` (dry-run, raport tabelaryczny).
- [ ] `CanaryStrategy` + `SingleStrategy`.
- [ ] `StateMachine` + SQLite models (`rollouts`, `rollout_hosts`, `rollout_steps`).
- [ ] `Executor` — snapshot placeholder (log "snapshot skipped"), update, reboot wait, health checks (`systemd`, `http`).
- [ ] Health check pass/fail logic + zatrzymanie rolloutu.
- [ ] `status`, `history` CLI.
- [ ] Recovery: wznawianie przerwanego rolloutu.
- [ ] Unit tests dla parsera, strategii, state machine.
- [ ] Integration test z 1 kontenerem Ubuntu + systemd (Docker privileged).
- [ ] README z instrukcją i przykładem.

### Wersja 2 — 3-4 tygodnie
**Cel**: Multi-distro, snapshoty, Prometheus, maintenance windows, powiadomienia.

- [ ] `DnfPackageManager` + `PacmanPackageManager`.
- [ ] `SnapshotDetector` + `BtrfsSnapshotProvider`, `LvmSnapshotProvider`, `ZfsSnapshotProvider`.
- [ ] Automatyczny rollback (LVM/ZFS online, Btrfs via reboot).
- [ ] `JournalCheck` + `TcpCheck` + `CommandCheck`.
- [ ] `MaintenanceWindow` + blokada poza oknem.
- [ ] Metryki Prometheus + textfile collector.
- [ ] Powiadomienia Slack/Discord (webhook po zakończeniu rolloutu).
- [ ] `BatchStrategy`.
- [ ] Integration tests na 3 dystrybucjach w CI.

### Wersja 3 — 2-3 tygodnie
**Cel**: Bezpieczeństwo, skalowalność, enterprise features.

- [ ] Podpisywanie konfiguracji YAML (GPG / minisign).
- [ ] Role użytkowników i RBAC (kto może deployować na produkcję).
- [ ] Approval workflow: plan musi być zaakceptowany przed deploy.
- [ ] Hash chain w audycie.
- [ ] Migracja do PostgreSQL (opcjonalna, przez config).
- [ ] Webowy panel (FastAPI + prosty HTML/HTMX lub React) do przeglądania rolloutów.
- [ ] Aktualizacje obrazowe: integracja z `systemd-sysupdate` (A/B partition updates).
- [ ] Skalowanie: worker queue (Celery / RQ) dla setek hostów.

---

## 21. Edge Cases & Failure Scenarios

### Lista scenariuszy awaryjnych i reakcji

| Scenariusz | Wykrycie | Reakcja |
|------------|----------|----------|
| **SSH nieosiągalny przed startem** | Connect timeout przy `Planner` | Oznacz hosta jako `unreachable` w planie. Deploy pomija go (lub abortuje, wg flagi `--abort-on-unreachable`). |
| **SSH znika w trakcie aktualizacji** | `asyncssh` disconnect podczas `apply_updates` | Zapisz stan jako `in_progress`. Po reconnect (retry 3x) sprawdź czy apt/dnf nadal działa (`lsof /var/lib/dpkg/lock`). Jeśli nie — uznaj za FAIL i ewentualnie rollback. |
| **Host się rebootuje ale nie wraca** | Poll SSH po reboot, timeout 300s | Status `FAILED`. Rollback niemożliwy (host nieosiągalny). Zatrzymaj rollout. Operator musi ręcznie naprawić hosta. |
| **Health check FAIL na kanarku** | HttpCheck 503 po aktualizacji | Zatrzymaj cały rollout. Automatyczny rollback kanarka. Reszta hostów = `skipped`. |
| **Health check FAIL na zwykłym hoście (nie kanarek)** | HttpCheck 503 w batchu | Zatrzymaj rollout. Rollback zepsutego hosta. Reszta batcha (jeśli jeszcze nie zaczęta) = skipped. Jeśli reszta batcha już w trakcie — dokończ ich update, ale nie przechodź do następnego batcha. |
| **Snapshot timeout** | `lvcreate` wisi > 120s | Jeśli `snapshot.on_unavailable == warn` — kontynuuj bez snapshotu (większe ryzyko). Jeśli `abort` — FAIL hosta, zatrzymaj rollout. |
| **Apt lock (`dpkg` zajęty przez unattended-upgrades)** | `apt-get` zwraca błąd lock | Retry co 30s, max 5 prób. Jeśli ciągle zajęte — FAIL hosta. |
| **Dwóch operatorów uruchamia deploy jednocześnie** | `rollout_locks` conflict | Drugi dostaje błąd: "Rollout already in progress for environment 'production' by <user> at <time>. Use --resume or wait." |
| **Zmiana czasu / DST w trakcie rolloutu** | System clock jump | Używamy `time.monotonic()` dla wewnętrznych timeoutów. Maintenance window opiera się na `datetime.now(timezone)` — jeśli DST przeskoczy, możemy wyjść poza okno. Decyzja: dokończ aktualny batch, nie zaczynaj nowego. |
| **Brak miejsca na snapshot LVM** | `lvcreate` zwraca "Insufficient free space" | FAIL hosta, zatrzymaj (jeśli `abort`) lub kontynuuj bez snapshotu (jeśli `warn`). |
| **Błąd jednego pakietu (dependency break)** | `apt` zwraca błąd kodu 100 | `UpdateResult.success=False`. Host = FAILED. Rollback (jeśli snapshot istnieje). |
| **Operator zabija proces PatchPilota (Ctrl+C)** | `KeyboardInterrupt` / `SIGTERM` | Handler sygnału: zapisz `rollout.status='paused'`, zwolnij locki, wyświetl komunikat: "Rollout paused. Run `patchpilot resume <id>` to continue." |
| **Health check PASS, ale aplikacja jest "zimna" (cold start)** | HTTP 200, ale czas odpowiedzi 30s | Dodaj `expected_response_time_ms` do HttpCheck. Lub retry z backoff (np. 3 próby co 10s). |
| **Zgodność kernela — apt zaktualizował kernel, ale reboot-required nie istnieje** | `requires_reboot()` sprawdza wersję kernela | Porównanie `uname -r` z `dpkg -l linux-image-generic`. Jeśli mismatch → reboot_required=True. |

---

## 22. Decyzje architektoniczne (ADR)

### ADR-1: SQLite zamiast plików JSON na stan
**Kontekst**: Musimy przechowywać stan rolloutów między uruchomieniami CLI (recovery, resume).
**Opcje**: Pliki JSON per rollout, SQLite, Redis.
**Decyzja**: SQLite.
**Uzasadnienie**: Zero infrastruktury, ACID, transakcje (locki), możliwość zapytań SQL, prosta migracja do PostgreSQL przez SQLAlchemy.

### ADR-2: asyncssh zamiast paramiko / subprocess ssh
**Kontekst**: Potrzebujemy wielu równoległych połączeń SSH z timeoutami.
**Opcje**: paramiko (sync, własne wątki), subprocess `ssh` (trudno parsować streamy), asyncssh.
**Decyzja**: asyncssh.
**Uzasadnienie**: Natywne asyncio, wysokopoziomowe API, wsparcie dla ED25519, proxy jump, multiplexing. Mniej boilerplate niż paramiko w async wrap.

### ADR-3: Agentless zamiast agenta na hoście
**Kontekst**: Wdrażanie na setki istniejących serwerów bez instalacji oprogramowania.
**Opcje**: Agent (daemon), agentless (SSH).
**Decyzja**: Agentless.
**Uzasadnienie**: Niższy próg wejścia, zero konfliktów z istniejącymi CM (Ansible, Chef), brak problemu z aktualizacją samego agenta.

### ADR-4: Strategia snapshot/rollback na poziomie systemu plików, nie aplikacji
**Kontekst**: Rollback musi przywrócić stan systemu sprzed aktualizacji.
**Opcje**: Backup configów + downgrade pakietów, snapshot systemu plików, VM snapshot.
**Decyzja**: Snapshot systemu plików (Btrfs/LVM/ZFS) + downgrade jeśli snapshot niemożliwy.
**Uzasadnienie**: Szybszy i pewniejszy niż downgrade apt/dnf (dependency hell). Daje atomowy powrót.

### ADR-5: Textfile collector zamiast Pushgateway dla metryk
**Kontekst**: Prometheus metryki per-host, per-rollout.
**Opcje**: Pushgateway, textfile collector, własny endpoint HTTP.
**Decyzja**: Textfile collector Node Exportera.
**Uzasadnienie**: Rekomendacja Prometheusa dla batch jobs. Nie wymaga dodatkowej usługi. Naturalnie pasuje do monitoringu serwerów.

### ADR-6: Health checki per-rola, nie per-host w kodzie
**Kontekst**: Różne role (api, db) mają różne wymagania co do "zdrowia".
**Opcje**: Hardcoded checki w kodzie Python, konfiguracja YAML per-rola.
**Decyzja**: Konfiguracja YAML.
**Uzasadnienie**: PatchPilot ma być narzędziem dla różnych środowisk. Hardcoded checki wymagałyby forkowania kodu na każdy projekt.

---

## 23. Zagrożenia i ograniczenia (znane na start)

1. **Btrfs rollback online jest trudny** — wymaga rebootu do snapshotu. W MVP rollback Btrfs może być ręczny lub wymagać `grub-btrfs`.
2. **LVM snapshot wymaga miejsca w VG** — jeśli VG jest pełny, snapshot się nie uda.
3. **Timeout rebootu** — jeśli kernel panic po aktualizacji, host nigdy nie wróci. PatchPilot tego nie naprawi.
4. **Nie obsługuje aktualizacji samego PatchPilot** — narzędzie na maszynie sterującej musi być aktualizowane ręcznie.
5. **Snapshot nie chroni przed błędami w firmware / BIOS** — ale to poza zakresem.
6. **Zależność od SSH** — jeśli SSH jest zepsuty przed aktualizacją, PatchPilot nie zadziała. Wymagany jest out-of-band access (IPMI, serial console) dla naprawy.
7. **Race condition przy równoległych operacjach na tym samym hoście** — rozwiązane przez 1 semaphore per host per rollout.

---

*Plan architektoniczny PatchPilot v1.0 — szczegółowy.*
