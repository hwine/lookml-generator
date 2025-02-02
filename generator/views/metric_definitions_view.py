"""Class to describe a view with metrics from metric-hub."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional, Union

from generator.metrics_utils import MetricsConfigLoader

from . import lookml_utils
from .table_view import TableView
from .view import View, ViewDict


class MetricDefinitionsView(View):
    """A view for metric-hub metrics that come from the same data source."""

    type: str = "metric_definitions_view"

    def __init__(self, namespace: str, name: str, tables: List[Dict[str, str]]):
        """Get an instance of an MetricDefinitionsView."""
        super().__init__(namespace, name, MetricDefinitionsView.type, tables)

    @classmethod
    def from_db_views(
        klass,
        namespace: str,
        is_glean: bool,
        channels: List[Dict[str, str]],
        db_views: dict,
    ) -> Iterator[MetricDefinitionsView]:
        """Get Metric Definition Views from db views and app variants."""
        return iter(())

    @classmethod
    def from_dict(
        klass, namespace: str, name: str, definition: ViewDict
    ) -> MetricDefinitionsView:
        """Get a MetricDefinitionsView from a dict representation."""
        return klass(namespace, name, definition.get("tables", []))

    def to_lookml(self, bq_client, v1_name: Optional[str]) -> Dict[str, Any]:
        """Get this view as LookML."""
        namespace_definitions = MetricsConfigLoader.configs.get_platform_definitions(
            self.namespace
        )
        if namespace_definitions is None:
            return {}

        # get all metric definitions that depend on the data source represented by this view
        data_source_name = re.sub("^metric_definitions_", "", self.name)
        data_source_definition = MetricsConfigLoader.configs.get_data_source_definition(
            data_source_name, self.namespace
        )

        if data_source_definition is None:
            return {}

        # todo: hide deprecated metrics?
        metric_definitions = [
            f"""{
                MetricsConfigLoader.configs.get_env().from_string(metric.select_expression).render()
            } AS {metric_slug},\n"""
            for metric_slug, metric in namespace_definitions.metrics.definitions.items()
            if metric.select_expression
            and metric.data_source.name == data_source_name
            and metric.type != "histogram"
        ]

        if metric_definitions == []:
            return {}

        # Metric definitions are intended to aggregated by client per date.
        # A derived table is needed to do these aggregations, instead of defining them as measures
        # we want to have them available as dimensions (which don't allow aggregations in their definitions)
        # to allow for custom measures to be later defined in Looker that aggregate these per client metrics.
        view_defn: Dict[str, Any] = {"name": self.name}

        ignore_base_fields = [
            "client_id",
            "submission_date",
            "submission",
            "first_run",
        ] + [
            metric_slug
            for metric_slug, metric in namespace_definitions.metrics.definitions.items()
            if metric.select_expression
            and metric.data_source.name == data_source_name
            and metric.type != "histogram"
        ]
        base_view_fields = []
        base_view_lkml = None
        join_base_view = ""
        if len(self.tables) > 0:
            base_table = self.tables[0]["table"]
            base_view = TableView(
                self.namespace,
                "base_view",
                [{"table": base_table, "channel": "release"}],
            )
            base_view_lkml = base_view.to_lookml(bq_client=bq_client, v1_name=None)

            base_view_fields = [
                f"{d['name']},\n"
                for d in base_view_lkml["views"][0]["dimensions"]
                if d["name"] not in ignore_base_fields
            ]

            join_base_view = f"""
            INNER JOIN {base_table} base
            ON
                base.submission_date = m.{data_source_definition.submission_date_column or "submission_date"} AND
                base.client_id = m.{data_source_definition.client_id_column or "client_id"}
            WHERE base.submission_date BETWEEN
                SAFE_CAST(
                    {{% date_start {data_source_definition.submission_date_column or "submission_date"} %}} AS DATE
                ) AND
                SAFE_CAST(
                    {{% date_end {data_source_definition.submission_date_column or "submission_date"} %}} AS DATE
                )
            """

        view_defn["derived_table"] = {
            "sql": f"""
            SELECT
                {"".join(metric_definitions)}
                {"base.".join(base_view_fields)}
                m.{data_source_definition.client_id_column or "client_id"} AS client_id,
                {{% if aggregate_metrics_by._parameter_value == 'day' %}}
                m.{data_source_definition.submission_date_column or "submission_date"} AS analysis_basis
                {{% elsif aggregate_metrics_by._parameter_value == 'week'  %}}
                (FORMAT_DATE(
                    '%F',
                    DATE_TRUNC(m.{data_source_definition.submission_date_column or "submission_date"},
                    WEEK(MONDAY)))
                ) AS analysis_basis
                {{% elsif aggregate_metrics_by._parameter_value == 'month'  %}}
                (FORMAT_DATE(
                    '%Y-%m',
                    m.{data_source_definition.submission_date_column or "submission_date"})
                ) AS analysis_basis
                {{% elsif aggregate_metrics_by._parameter_value == 'quarter'  %}}
                (FORMAT_DATE(
                    '%Y-%m',
                    DATE_TRUNC(m.{data_source_definition.submission_date_column or "submission_date"},
                    QUARTER))
                ) AS analysis_basis
                {{% elsif aggregate_metrics_by._parameter_value == 'year'  %}}
                (EXTRACT(
                    YEAR FROM m.{data_source_definition.submission_date_column or "submission_date"})
                ) AS analysis_basis
                {{% else %}}
                NULL as analysis_basis
                {{% endif %}}
            FROM
                {
                    MetricsConfigLoader.configs.get_data_source_sql(
                        data_source_name,
                        self.namespace
                    ).format(dataset=self.namespace)
                }
            AS m
            {join_base_view}
            {'AND' if join_base_view else 'WHERE'} m.submission_date BETWEEN
                SAFE_CAST(
                    {{% date_start {data_source_definition.submission_date_column or "submission_date"} %}} AS DATE
                ) AND
                SAFE_CAST(
                    {{% date_end {data_source_definition.submission_date_column or "submission_date"} %}} AS DATE
                )
            GROUP BY
                {"".join(base_view_fields)}
                client_id,
                analysis_basis
            """
        }

        view_defn["dimensions"] = self.get_dimensions()
        view_defn["dimension_groups"] = self.get_dimension_groups()

        if base_view_lkml:
            for dimension in base_view_lkml["views"][0]["dimensions"]:
                if dimension["name"] not in ignore_base_fields:
                    dimension["group_label"] = "Base Fields"
                    view_defn["dimensions"].append(dimension)

            for dimension_group in base_view_lkml["views"][0]["dimension_groups"]:
                if dimension_group["name"] not in ignore_base_fields:
                    dimension_group["group_label"] = "Base Fields"
                    view_defn["dimension_groups"].append(dimension_group)

        view_defn["measures"] = self.get_measures(
            view_defn["dimensions"],
        )
        view_defn["sets"] = self._get_sets()
        view_defn["parameters"] = self._get_parameters()

        return {"views": [view_defn]}

    def get_dimensions(
        self, _bq_client=None, _table=None, _v1_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get the set of dimensions for this view based on the metric definitions in metric-hub."""
        namespace_definitions = MetricsConfigLoader.configs.get_platform_definitions(
            self.namespace
        )
        metric_definitions = namespace_definitions.metrics.definitions
        data_source_name = re.sub("^metric_definitions_", "", self.name)

        return [
            {
                "name": "client_id",
                "type": "string",
                "sql": "SAFE_CAST(${TABLE}.client_id AS STRING)",
                "label": "Client ID",
                "primary_key": "yes",
                "group_label": "Base Fields",
                "description": "Unique client identifier",
            },
        ] + [  # add a dimension for each metric definition
            {
                "name": metric_slug,
                "group_label": "Metrics",
                "label": metric.friendly_name
                or lookml_utils.slug_to_title(metric_slug),
                "description": metric.description or "",
                "type": "number",
                "sql": "${TABLE}." + metric_slug,
            }
            for metric_slug, metric in metric_definitions.items()
            if metric.select_expression
            and metric.data_source.name == data_source_name
            and metric.type != "histogram"
        ]

    def get_dimension_groups(self) -> List[Dict[str, Any]]:
        """Get dimension groups for this view."""
        return [
            {
                "name": "submission",
                "type": "time",
                "group_label": "Base Fields",
                "sql": "CAST(${TABLE}.analysis_basis AS TIMESTAMP)",
                "label": "Submission",
                "timeframes": [
                    "raw",
                    "date",
                    "week",
                    "month",
                    "quarter",
                    "year",
                ],
            }
        ]

    def _get_sets(self) -> List[Dict[str, Any]]:
        """Generate metric sets."""
        # group all the metric dimensions into a set
        dimensions = self.get_dimensions()
        measures = self.get_measures(dimensions)

        return [
            {
                "name": "metrics",
                "fields": [
                    dimension["name"]
                    for dimension in dimensions
                    if dimension["name"] != "client_id"
                ]
                + [measure["name"] for measure in measures],
            }
        ]

    def _get_parameters(self):
        return [
            {
                "name": "aggregate_metrics_by",
                "label": "Aggregate Client Metrics Per",
                "type": "unquoted",
                "default_value": "day",
                "allowed_values": [
                    {"label": "Per Day", "value": "day"},
                    {"label": "Per Week", "value": "week"},
                    {"label": "Per Month", "value": "month"},
                    {"label": "Per Quarter", "value": "quarter"},
                    {"label": "Per Year", "value": "year"},
                    {"label": "Overall", "value": "overall"},
                ],
            }
        ]

    def get_measures(
        self, dimensions: List[dict]
    ) -> List[Dict[str, Union[str, List[Dict[str, str]]]]]:
        """Get statistics as measures."""
        measures = []
        for dimension in dimensions:
            metric = MetricsConfigLoader.configs.get_metric_definition(
                dimension["name"], self.namespace
            )
            if metric and metric.statistics:
                for statistic_slug, _ in metric.statistics.items():
                    if statistic_slug == "sum":
                        measures.append(
                            {
                                "name": f"{dimension['name']}_{statistic_slug}",
                                "type": "sum",
                                "sql": "${TABLE}." + dimension["name"],
                                "label": f"{dimension['label']} Sum",
                                "group_label": "Statistics",
                                "description": f"Sum of {dimension['label']}",
                            }
                        )
                    elif statistic_slug == "client_count":
                        measures.append(
                            {
                                "name": f"{dimension['name']}_{statistic_slug}",
                                "type": "count_distinct",
                                "label": f"{dimension['label']} Client Count",
                                "group_label": "Statistics",
                                "sql": "IF(SAFE_CAST(${TABLE}."
                                + f"{dimension['name']} AS BOOL), "
                                + "${TABLE}.client_id, SAFE_CAST(NULL AS STRING))",
                                "description": f"Number of clients with {dimension['label']}",
                            }
                        )

        return measures
