import io
from datetime import datetime

from patchpilot.db.models import Rollout, RolloutHost

METRIC_HEADER_TEMPLATE = """# HELP {prefix}last_update_timestamp Unix timestamp of last update per host
# TYPE {prefix}last_update_timestamp gauge
{last_timestamp}
# HELP {prefix}update_success Whether the last rollout was successful (1=success, 0=failure)
# TYPE {prefix}update_success gauge
{success}
# HELP {prefix}packages_updated_total Total packages updated per host
# TYPE {prefix}packages_updated_total counter
{packages}
# HELP {prefix}rollback_total Total rollbacks per host
# TYPE {prefix}rollback_total counter
{rollbacks}
# HELP {prefix}reboot_required Whether reboot was required (1=yes, 0=no)
# TYPE {prefix}reboot_required gauge
{reboot}
# HELP {prefix}rollout_duration_seconds Duration of the rollout in seconds
# TYPE {prefix}rollout_duration_seconds gauge
{duration}
"""


def generate_metrics(
    rollout: Rollout,
    hosts: list[RolloutHost],
    prefix: str = "patchpilot_",
) -> str:
    buf = io.StringIO()

    lines: list[str] = []
    success_lines: list[str] = []
    packages_lines: list[str] = []
    rollback_lines: list[str] = []
    reboot_lines: list[str] = []

    now_ts = int(datetime.utcnow().timestamp())

    for host in hosts:
        labels = f'host="{host.host_name}",env="{rollout.env_name}",role="{host.host_role}"'

        lines.append(f'{prefix}last_update_timestamp{{{labels}}} {now_ts}')

        is_healthy = 1 if host.status == "healthy" else 0
        success_lines.append(f'{prefix}update_success{{{labels}}} {is_healthy}')

        # Count packages from the update step — simplified: 1 per host
        packages_lines.append(f'{prefix}packages_updated_total{{{labels}}} 1')

        is_rolled_back = 1 if host.status == "rolled_back" else 0
        rollback_lines.append(f'{prefix}rollback_total{{{labels}}} {is_rolled_back}')

        needs_reboot = 1 if host.reboot_required else 0
        reboot_lines.append(f'{prefix}reboot_required{{{labels}}} {needs_reboot}')

    duration = 0.0
    if rollout.started_at and rollout.finished_at:
        duration = (rollout.finished_at - rollout.started_at).total_seconds()

    duration_line = (
        f'{prefix}rollout_duration_seconds'
        f'{{env="{rollout.env_name}",rollout="{rollout.id}"}} {duration}'
    )

    buf.write(
        METRIC_HEADER_TEMPLATE.format(
            prefix=prefix,
            last_timestamp="\n".join(lines),
            success="\n".join(success_lines),
            packages="\n".join(packages_lines),
            rollbacks="\n".join(rollback_lines),
            reboot="\n".join(reboot_lines),
            duration=duration_line,
        )
    )

    return buf.getvalue()
