from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    IPvAnyAddress,
    SecretStr,
    field_validator,
    model_validator,
)


class RetryConfig(BaseModel):
    max_attempts: int = Field(default=3, ge=0, le=10)
    backoff_seconds: float = Field(default=5, ge=0)


class ConnectionSettings(BaseModel):
    ssh_user: str = Field(default="root")
    ssh_key_path: Path | None = Field(default=None)
    ssh_timeout: int = Field(default=30, ge=1, le=120)
    sudo: bool = Field(default=True)
    sudo_password: SecretStr | None = Field(default=None)
    parallel_limit: int = Field(default=5, ge=1, le=100)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @field_validator("ssh_key_path")
    @classmethod
    def check_key_path(cls, v: Path | None) -> Path | None:
        if v is not None:
            resolved = v.expanduser()
            if not resolved.exists():
                return v
            mode = resolved.stat().st_mode
            if mode & 0o077:
                import warnings
                warnings.warn(
                    f"SSH key {resolved} has permissive permissions ({oct(mode & 0o777)}). "
                    f"Expected 0o600.", stacklevel=2
                )
        return v


class HealthCheckModel(BaseModel):
    type: Literal["systemd", "http", "tcp", "command", "journal"]
    # systemd
    service: str | None = Field(default=None)
    state: str | None = Field(default="active")
    # http
    url: str | None = Field(default=None)
    method: str = Field(default="GET")
    expected_status: int = Field(default=200)
    # tcp
    host: str | None = Field(default="localhost")
    port: int | None = Field(default=None)
    # command
    command: str | None = Field(default=None)
    expected_exit_code: int = Field(default=0)
    # journal
    forbidden_patterns: list[str] = Field(default_factory=list)
    lookback_seconds: int = Field(default=60)
    # general
    timeout: int = Field(default=30, ge=1, le=300)
    retries: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="after")
    def validate_required_fields(self) -> "HealthCheckModel":
        if self.type == "systemd" and not self.service:
            raise ValueError("systemd health check requires 'service' field")
        if self.type == "http" and not self.url:
            raise ValueError("http health check requires 'url' field")
        if self.type == "tcp" and not self.port:
            raise ValueError("tcp health check requires 'port' field")
        if self.type == "command" and not self.command:
            raise ValueError("command health check requires 'command' field")
        if self.type == "journal" and not self.service:
            raise ValueError("journal health check requires 'service' field")
        return self


class HealthCheckConfig(BaseModel):
    global_: list[HealthCheckModel] = Field(
        default_factory=list, alias="global"
    )
    per_role: dict[str, list[HealthCheckModel]] = Field(default_factory=dict)

    def for_role(self, role: str) -> list[HealthCheckModel]:
        return self.global_ + self.per_role.get(role, [])


class HostModel(BaseModel):
    name: str
    address: IPvAnyAddress | str
    role: str = Field(default="default")
    ssh_user: str | None = Field(default=None)
    ssh_key_path: Path | None = Field(default=None)
    ssh_port: int = Field(default=22)
    sudo: bool | None = Field(default=None)
    sudo_password: SecretStr | None = Field(default=None)
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Host name must not be empty")
        return v


class CanaryConfig(BaseModel):
    count: int = Field(default=1, ge=1)
    tag_filter: str | None = Field(default=None)


class BatchConfig(BaseModel):
    size: int = Field(default=2, ge=1)
    wait_for_batch: bool = Field(default=True)
    health_check_timeout: int = Field(default=300, ge=30)


class StrategyModel(BaseModel):
    type: Literal["canary", "batch", "single"] = Field(default="canary")
    canary: CanaryConfig | None = Field(default=None)
    batch: BatchConfig | None = Field(default=None)


class SnapshotConfig(BaseModel):
    preferred: str = Field(default="auto")
    on_unavailable: Literal["warn", "abort"] = Field(default="warn")
    timeout: int = Field(default=120, ge=10)


class MaintenanceWindowDef(BaseModel):
    start: str
    end: str
    days: list[Literal[
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ]]


class MaintenanceConfig(BaseModel):
    timezone: str = Field(default="UTC")
    windows: list[MaintenanceWindowDef] = Field(default_factory=list)
    plan_outside_window: bool = Field(default=True)
    deploy_outside_window: bool = Field(default=False)


class AuditConfig(BaseModel):
    enabled: bool = Field(default=True)
    db_path: str | None = Field(default=None)
    hash_chain: bool = Field(default=False)


class MetricsConfig(BaseModel):
    enabled: bool = Field(default=False)
    textfile_directory: str | None = Field(default=None)
    prefix: str = Field(default="patchpilot_")


class InventoryMetadata(BaseModel):
    name: str
    description: str | None = Field(default=None)
    owner: str | None = Field(default=None)


class InventoryModel(BaseModel):
    metadata_: InventoryMetadata = Field(alias="metadata")
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    hosts: list[HostModel]
    strategy: StrategyModel = Field(default_factory=StrategyModel)
    health_checks: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    @model_validator(mode="after")
    def validate_hosts(self) -> "InventoryModel":
        names = [h.name for h in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("Host names must be unique within an environment")
        return self

    def hosts_by_role(self, role: str) -> list[HostModel]:
        return [h for h in self.hosts if h.role == role]

    def hosts_by_tag(self, tag: str) -> list[HostModel]:
        return [h for h in self.hosts if tag in h.tags]


class InventoryLoader:
    @staticmethod
    def from_yaml(path: str | Path) -> "InventoryModel":
        import yaml

        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Inventory file not found: {path}")

        with open(path) as f:
            raw = yaml.safe_load(f)

        return InventoryModel.model_validate(raw)

    @staticmethod
    def default_path(name: str = "production") -> Path:
        return Path.home() / ".config" / "patchpilot" / f"{name}.yaml"
