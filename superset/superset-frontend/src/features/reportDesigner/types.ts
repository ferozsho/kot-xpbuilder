/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

export type DatasetColumn = {
  column_name: string;
  type?: string | null;
  is_dttm: boolean;
  expression?: string | null;
};

export type DatasetMetric = {
  metric_name: string;
  expression: string;
};

export type DatasetOption = {
  id: number;
  table_name: string;
  schema?: string | null;
  catalog?: string | null;
  database_name: string;
  columns: DatasetColumn[];
  metrics: DatasetMetric[];
};

export type DatasetRef = {
  id: number;
  alias: string;
};

export type Relationship = {
  left_dataset: number;
  left_column: string;
  right_dataset: number;
  right_column: string;
  join_type: 'INNER' | 'LEFT' | 'FULL';
};

export type FieldRef = {
  dataset: number;
  column: string;
  label: string;
};

export type MetricRef = {
  dataset: number;
  column: string;
  aggregate: 'SUM' | 'AVG' | 'COUNT' | 'COUNT_DISTINCT' | 'MIN' | 'MAX';
  label: string;
};

export type FilterRef = {
  dataset: number;
  column: string;
  op: string;
  value: string | string[];
};

export type OrderRef = {
  dataset: number;
  column: string;
  desc: boolean;
  /** When ordering by an aggregated metric, reference the metric label. */
  label?: string;
};

export type ReportDefinition = {
  version: number;
  datasets: DatasetRef[];
  relationships: Relationship[];
  columns: FieldRef[];
  metrics: MetricRef[];
  group_by: FieldRef[];
  filters: FilterRef[];
  order_by: OrderRef[];
  limit: number;
};

export type Report = {
  id: number;
  name: string;
  description: string;
  definition: ReportDefinition;
  changed_on?: string | null;
  created_on?: string | null;
  /** Publish metadata — set when the report is published to Superset. */
  dataset_id?: number | null;
  chart_id?: number | null;
  dashboard_id?: number | null;
  viz_type?: string | null;
  chart_name?: string | null;
  published_at?: string | null;
};

export type DashboardOption = {
  id: number;
  dashboard_title: string;
};

export type VizTypeOption = {
  key: string;
  label: string;
  requires_dttm: boolean;
};

export type PublishResult = {
  report_id: number;
  dataset_id: number;
  chart_id: number;
  viz_type: string;
  chart_name: string;
  dashboard_id: number | null;
  dashboard_title: string | null;
  explore_url: string;
  dashboard_url: string | null;
};

export type PublishPayload = {
  viz_type: string;
  chart_name?: string;
  dashboard_id?: number;
  new_dashboard_name?: string;
};

export type PreviewResult = {
  sql: string;
  columns: string[];
  rows: Record<string, unknown>[];
  count: number;
  truncated: boolean;
};

export const EMPTY_DEFINITION: ReportDefinition = {
  version: 1,
  datasets: [],
  relationships: [],
  columns: [],
  metrics: [],
  group_by: [],
  filters: [],
  order_by: [],
  limit: 1000,
};

export const FILTER_OPS = [
  '=',
  '!=',
  '>',
  '>=',
  '<',
  '<=',
  'LIKE',
  'IN',
  'NOT IN',
  'IS NULL',
  'IS NOT NULL',
  'BETWEEN',
];

export const AGGREGATES = [
  'SUM',
  'AVG',
  'COUNT',
  'COUNT_DISTINCT',
  'MIN',
  'MAX',
];

export const JOIN_TYPES = ['INNER', 'LEFT', 'FULL'];
