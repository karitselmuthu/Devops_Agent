"""AWS CloudWatch metric threshold checks for health and SLO gates."""

import json
from datetime import datetime, timedelta, timezone

from agents_shared.models import AgentKind, CloudWatchMetricTarget, WorkflowTask


class MonitoringAgent:
    def __init__(self, clients: dict[str, object] | None = None) -> None:
        self.clients = clients if clients is not None else {}

    def _client(self, region: str) -> object:
        if region not in self.clients:
            import boto3

            self.clients[region] = boto3.Session(region_name=region).client("cloudwatch")
        return self.clients[region]

    def execute(self, task: WorkflowTask) -> str:
        if task.agent != AgentKind.MONITORING or task.monitoring_target is None:
            raise ValueError("Monitoring task requires a CloudWatch metric target")
        target: CloudWatchMetricTarget = task.monitoring_target
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=target.lookback_minutes)
        response = self._client(target.region).get_metric_statistics(
            Namespace=target.namespace,
            MetricName=target.metric_name,
            Dimensions=[{"Name": name, "Value": value} for name, value in target.dimensions.items()],
            StartTime=start,
            EndTime=end,
            Period=target.period_seconds,
            Statistics=[target.statistic],
        )
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            raise RuntimeError("CloudWatch returned no metric data in the evaluation window")
        values = [float(point[target.statistic]) for point in datapoints]
        if target.statistic == "Average":
            counts = [float(point.get("SampleCount", 1)) for point in datapoints]
            observed = sum(value * count for value, count in zip(values, counts)) / sum(counts)
        elif target.statistic in {"Sum", "SampleCount"}:
            observed = sum(values)
        elif target.statistic == "Minimum":
            observed = min(values)
        else:
            observed = max(values)
        healthy = (
            observed <= target.threshold
            if target.comparison == "lte"
            else observed >= target.threshold
        )
        report = json.dumps(
            {
                "metric": f"{target.namespace}/{target.metric_name}",
                "observed": observed,
                "threshold": target.threshold,
                "comparison": target.comparison,
                "status": "HEALTHY" if healthy else "UNHEALTHY",
            }
        )
        if not healthy:
            raise RuntimeError(report)
        return report
