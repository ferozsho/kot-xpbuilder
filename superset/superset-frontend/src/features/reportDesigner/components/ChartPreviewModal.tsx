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
import { useEffect, useMemo, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Empty,
  InputNumber,
  Loading,
  Modal,
  Select,
  Space,
  Table,
  Typography,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { previewReport } from '../api';
import { PreviewResult, ReportDefinition } from '../types';

const PREVIEW_WIDTH = 560;
const PREVIEW_HEIGHT = 280;

const PALETTE = [
  '#4f46e5',
  '#06b6d4',
  '#f59e0b',
  '#10b981',
  '#ef4444',
  '#8b5cf6',
  '#ec4899',
  '#14b8a6',
  '#f97316',
  '#6366f1',
];

const toNum = (value: unknown): number => {
  const n =
    typeof value === 'number'
      ? value
      : Number.parseFloat(String(value ?? '').replace(/[,%$]/g, ''));
  return Number.isFinite(n) ? n : 0;
};

const fmtNum = (value: unknown): string => {
  const n = toNum(value);
  if (Number.isInteger(n)) {
    return n.toLocaleString();
  }
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
};

const shortLabel = (label: string, max = 14): string =>
  label.length > max ? `${label.slice(0, max - 1)}…` : label;

type Props = {
  open: boolean;
  onClose: () => void;
  onPublish: () => void;
  definition: ReportDefinition;
  vizKey: string;
  vizLabel: string;
  chartName: string;
  publishing: boolean;
};

/**
 * Renders a live, professional preview of the chart a report will become,
 * using the report's own query result, with per-viz options.
 */
export default function ChartPreviewModal({
  open,
  onClose,
  onPublish,
  definition,
  vizKey,
  vizLabel,
  chartName,
  publishing,
}: Props) {
  const [result, setResult] = useState<PreviewResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [labelField, setLabelField] = useState<string | undefined>();
  const [valueField, setValueField] = useState<string | undefined>();
  const [rowLimit, setRowLimit] = useState(20);

  const isAggregate = !['table', 'big_number', 'big_number_total'].includes(
    vizKey,
  );

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    previewReport(definition)
      .then(next => {
        setResult(next);
        const labels = (definition.columns ?? []).map(
          item => item.label || item.column,
        );
        const metrics = (definition.metrics ?? []).map(
          item => item.label || `${item.aggregate}(${item.column})`,
        );
        // Prefer a metric label, then the last numeric-looking column.
        const numericCols = next.columns.filter(col =>
          next.rows.some(row => {
            const value = row[col];
            if (typeof value === 'number') return true;
            return (
              typeof value === 'string' &&
              /^-?[\d.,]+$/.test(String(value).trim())
            );
          }),
        );
        setLabelField(labels[0] ?? next.columns[0]);
        setValueField(
          metrics[0] ??
            numericCols[numericCols.length - 1] ??
            next.columns[next.columns.length - 1],
        );
      })
      .catch(err => {
        setError(
          (err as { message?: string })?.message ||
            t('Could not preview the chart'),
        );
        setResult(null);
      })
      .finally(() => setLoading(false));
    // Refresh whenever the viz type changes too.
  }, [open, vizKey, definition]);

  const labelOptions = useMemo(() => {
    const labels = (definition.columns ?? []).map(
      item => item.label || item.column,
    );
    return [...new Set([...(result?.columns ?? []), ...labels])].map(label => ({
      label,
      value: label,
    }));
  }, [definition.columns, result]);

  const valueOptions = useMemo(() => {
    const metrics = (definition.metrics ?? []).map(
      item => item.label || `${item.aggregate}(${item.column})`,
    );
    return [...new Set([...(result?.columns ?? []), ...metrics])].map(
      label => ({ label, value: label }),
    );
  }, [definition.metrics, result]);

  const renderPreview = () => {
    if (!result) return null;
    const rows = result.rows;
    if (rows.length === 0) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('The report returned no rows to preview.')}
        />
      );
    }

    const label = labelField ?? result.columns[0];
    const value = valueField ?? result.columns[result.columns.length - 1];

    switch (vizKey) {
      case 'big_number':
      case 'big_number_total': {
        const first = rows[0];
        return (
          <BigNumberViz
            label={value}
            value={first ? fmtNum(first[value]) : '—'}
            caption={label}
          />
        );
      }
      case 'pie':
        return <PieViz rows={rows} labelKey={label} valueKey={value} />;
      case 'bar':
        return <BarViz rows={rows} labelKey={label} valueKey={value} />;
      case 'histogram':
        return <HistogramViz rows={rows} valueKey={value} />;
      case 'scatter':
        return <ScatterViz rows={rows} labelKey={label} valueKey={value} />;
      case 'line':
        return <LineViz rows={rows} labelKey={label} valueKey={value} fill={false} />;
      case 'area':
        return <LineViz rows={rows} labelKey={label} valueKey={value} fill />;
      case 'table':
        return (
          <TableViz
            columns={result.columns.slice(0, 8)}
            rows={rows.slice(0, rowLimit)}
          />
        );
      default:
        return (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t(
              'Live preview is not available for this chart type yet — '
                + 'publish it and open it in Explore to see the result.',
            )}
          />
        );
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={940}
      hideFooter
      destroyOnClose
    >
      <StyledModalBody>
        <StyledModalHead>
          <StyledVizTile>
            <VizGlyph vizKey={vizKey} />
          </StyledVizTile>
          <div>
            <Typography.Title level={5} style={{ margin: 0 }}>
              {chartName || t('Untitled chart')}
            </Typography.Title>
            <Typography.Text type="secondary">
              {vizLabel} · {t('preview from live report data')}
            </Typography.Text>
          </div>
        </StyledModalHead>

        <StyledGrid>
          <StyledPreviewPane>
            {loading ? (
              <StyledCenter>
                <Loading position="floating" />
              </StyledCenter>
            ) : error ? (
              <StyledCenter>
                <Typography.Text type="danger">{error}</Typography.Text>
              </StyledCenter>
            ) : (
              renderPreview()
            )}
          </StyledPreviewPane>

          <StyledOptionsPane>
            <Typography.Text strong style={{ display: 'block' }}>
              {t('Chart options')}
            </Typography.Text>
            {isAggregate ? (
              <>
                <StyledOption>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {t('Label / group by')}
                  </Typography.Text>
                  <Select
                    value={labelField}
                    options={labelOptions}
                    showSearch
                    style={{ width: '100%' }}
                    onChange={setLabelField}
                  />
                </StyledOption>
                <StyledOption>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {t('Value')}
                  </Typography.Text>
                  <Select
                    value={valueField}
                    options={valueOptions}
                    showSearch
                    style={{ width: '100%' }}
                    onChange={setValueField}
                  />
                </StyledOption>
              </>
            ) : vizKey === 'table' ? (
              <StyledOption>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {t('Rows shown')}
                </Typography.Text>
                <InputNumber
                  min={1}
                  max={200}
                  value={rowLimit}
                  onChange={value => setRowLimit(Number(value) || 20)}
                  style={{ width: '100%' }}
                />
              </StyledOption>
            ) : null}
            <StyledOption>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t('Report rows')}
              </Typography.Text>
              <Typography.Text strong>
                {result ? `${result.count}${result.truncated ? '+' : ''}` : '—'}
              </Typography.Text>
            </StyledOption>
          </StyledOptionsPane>
        </StyledGrid>

        <StyledModalFoot>
          <Space>
            <Button onClick={onClose}>{t('Cancel')}</Button>
            <Button
              type="primary"
              icon={<Icons.UploadOutlined />}
              loading={publishing}
              onClick={() => {
                onPublish();
                onClose();
              }}
            >
              {t('Publish this chart')}
            </Button>
          </Space>
        </StyledModalFoot>
      </StyledModalBody>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Mini visualizations (pure SVG, no charting dependency)
// ---------------------------------------------------------------------------

function VizGlyph({ vizKey }: { vizKey: string }) {
  switch (vizKey) {
    case 'pie':
      return <Icons.PieChartOutlined />;
    case 'bar':
    case 'histogram':
      return <Icons.BarChartOutlined />;
    case 'line':
      return <Icons.LineChartOutlined />;
    case 'area':
      return <Icons.AreaChartOutlined />;
    case 'big_number':
    case 'big_number_total':
      return <Icons.NumberOutlined />;
    case 'table':
      return <Icons.TableOutlined />;
    default:
      return <Icons.BarChartOutlined />;
  }
}

function BarViz({
  rows,
  labelKey,
  valueKey,
  width = PREVIEW_WIDTH,
  height = PREVIEW_HEIGHT,
}: {
  rows: Record<string, unknown>[];
  labelKey: string;
  valueKey: string;
  width?: number;
  height?: number;
}) {
  const data = rows.slice(0, 12);
  const max = Math.max(1, ...data.map(row => toNum(row[valueKey])));
  const padL = 46;
  const padB = 40;
  const innerW = width - padL - 16;
  const innerH = height - padB - 24;
  const slot = innerW / data.length;
  const barW = Math.min(46, slot * 0.6);

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {[0.25, 0.5, 0.75, 1].map(tick => {
        const y = 24 + innerH * (1 - tick);
        return (
          <g key={tick}>
            <line
              x1={padL}
              x2={width - 8}
              y1={y}
              y2={y}
              stroke="#e5e7eb"
              strokeDasharray="4 4"
            />
            <text x={padL - 6} y={y + 4} textAnchor="end" fontSize={10} fill="#9ca3af">
              {fmtNum(max * tick)}
            </text>
          </g>
        );
      })}
      {data.map((row, i) => {
        const value = toNum(row[valueKey]);
        const barH = Math.max(2, (value / max) * innerH);
        const x = padL + slot * i + (slot - barW) / 2;
        const y = 24 + innerH - barH;
        return (
          <g key={i}>
            <rect
              x={x}
              y={y}
              width={barW}
              height={barH}
              rx={4}
              fill={PALETTE[i % PALETTE.length]}
            />
            <text
              x={x + barW / 2}
              y={y - 6}
              textAnchor="middle"
              fontSize={10}
              fontWeight={600}
              fill="#374151"
            >
              {fmtNum(value)}
            </text>
            <text
              x={x + barW / 2}
              y={height - 12}
              textAnchor="middle"
              fontSize={10}
              fill="#6b7280"
            >
              {shortLabel(String(row[labelKey] ?? ''))}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function HistogramViz({
  rows,
  valueKey,
  width = PREVIEW_WIDTH,
  height = PREVIEW_HEIGHT,
}: {
  rows: Record<string, unknown>[];
  valueKey: string;
  width?: number;
  height?: number;
}) {
  const values = rows
    .map(row => toNum(row[valueKey]))
    .filter(n => Number.isFinite(n));
  const bins = 10;
  if (values.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const bucketSize = (max - min || 1) / bins;
  const counts = new Array<number>(bins).fill(0);
  values.forEach(v => {
    const idx = Math.min(bins - 1, Math.floor((v - min) / bucketSize));
    counts[idx] += 1;
  });
  const peak = Math.max(1, ...counts);
  const padL = 46;
  const padB = 36;
  const innerW = width - padL - 16;
  const innerH = height - padB - 24;
  const slot = innerW / bins;
  const barW = slot * 0.72;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {counts.map((count, i) => {
        const barH = (count / peak) * innerH;
        const x = padL + slot * i + (slot - barW) / 2;
        const y = 24 + innerH - barH;
        return (
          <g key={i}>
            <rect
              x={x}
              y={y}
              width={barW}
              height={Math.max(2, barH)}
              rx={3}
              fill="#4f46e5"
            />
            <text
              x={x + barW / 2}
              y={height - 12}
              textAnchor="middle"
              fontSize={9}
              fill="#6b7280"
            >
              {fmtNum(min + bucketSize * i)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function LineViz({
  rows,
  labelKey,
  valueKey,
  fill,
  width = PREVIEW_WIDTH,
  height = PREVIEW_HEIGHT,
}: {
  rows: Record<string, unknown>[];
  labelKey: string;
  valueKey: string;
  fill: boolean;
  width?: number;
  height?: number;
}) {
  const data = rows.slice(0, 40);
  const values = data.map(row => toNum(row[valueKey]));
  const min = Math.min(0, ...values);
  const max = Math.max(1, ...values);
  const padL = 46;
  const padB = 36;
  const innerW = width - padL - 16;
  const innerH = height - padB - 24;
  const stepX = data.length > 1 ? innerW / (data.length - 1) : innerW;

  const points = data.map((row, i) => {
    const x = padL + stepX * i;
    const y = 24 + innerH * (1 - (toNum(row[valueKey]) - min) / (max - min));
    return { x, y, label: String(row[labelKey] ?? ''), value: row[valueKey] };
  });

  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ');
  const areaPath =
    points.length > 0
      ? `${path} L${points[points.length - 1].x},${24 + innerH} L${points[0].x},${24 + innerH} Z`
      : '';

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {[0.25, 0.5, 0.75, 1].map(tick => {
        const y = 24 + innerH * (1 - tick);
        return (
          <g key={tick}>
            <line
              x1={padL}
              x2={width - 8}
              y1={y}
              y2={y}
              stroke="#e5e7eb"
              strokeDasharray="4 4"
            />
            <text x={padL - 6} y={y + 4} textAnchor="end" fontSize={10} fill="#9ca3af">
              {fmtNum(min + (max - min) * tick)}
            </text>
          </g>
        );
      })}
      {fill && <path d={areaPath} fill="#4f46e5" opacity={0.15} />}
      {points.length > 1 && (
        <path d={path} fill="none" stroke="#4f46e5" strokeWidth={2.5} strokeLinejoin="round" />
      )}
      {points.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={3.5} fill="#fff" stroke="#4f46e5" strokeWidth={2} />
      ))}
      {points.filter((_, i) => i % Math.ceil(points.length / 6) === 0).map((p, i) => (
        <text
          key={i}
          x={p.x}
          y={height - 10}
          textAnchor="middle"
          fontSize={9}
          fill="#6b7280"
        >
          {shortLabel(p.label, 8)}
        </text>
      ))}
    </svg>
  );
}

function PieViz({
  rows,
  labelKey,
  valueKey,
  width = PREVIEW_WIDTH,
  height = PREVIEW_HEIGHT,
}: {
  rows: Record<string, unknown>[];
  labelKey: string;
  valueKey: string;
  width?: number;
  height?: number;
}) {
  const data = rows.slice(0, 8).map(row => ({
    label: String(row[labelKey] ?? ''),
    value: Math.max(0, toNum(row[valueKey])),
  }));
  const total = data.reduce((sum, d) => sum + d.value, 0);
  const cx = 150;
  const cy = height / 2;
  const radius = Math.min(110, height / 2 - 30);

  let angle = -Math.PI / 2;
  const arcs = data.map((d, i) => {
    const start = angle;
    const sweep = total > 0 ? (d.value / total) * Math.PI * 2 : 0;
    angle += sweep;
    const large = sweep > Math.PI ? 1 : 0;
    const x0 = cx + radius * Math.cos(start);
    const y0 = cy + radius * Math.sin(start);
    const x1 = cx + radius * Math.cos(start + sweep);
    const y1 = cy + radius * Math.sin(start + sweep);
    const path =
      d.value > 0
        ? `M${cx},${cy} L${x0},${y0} A${radius},${radius} 0 ${large} 1 ${x1},${y1} Z`
        : '';
    return { path, color: PALETTE[i % PALETTE.length], label: d.label, value: d.value };
  });

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'center', height: '100%' }}>
      <svg width={width - 240} height={height} viewBox={`0 0 ${width - 240} ${height}`}>
        {arcs.map((arc, i) => (
          <path key={i} d={arc.path} fill={arc.color} stroke="#fff" strokeWidth={2} />
        ))}
        <text x={cx} y={cy - 4} textAnchor="middle" fontSize={22} fontWeight={700} fill="#374151">
          {fmtNum(total)}
        </text>
        <text x={cx} y={cy + 18} textAnchor="middle" fontSize={11} fill="#9ca3af">
          {t('total')}
        </text>
      </svg>
      <div>
        {arcs.map((arc, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span
              style={{
                width: 10,
                height: 10,
                borderRadius: 3,
                background: arc.color,
                display: 'inline-block',
              }}
            />
            <span style={{ fontSize: 12, color: '#374151', maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {arc.label || `#${i + 1}`}
            </span>
            <span style={{ fontSize: 12, fontWeight: 600, color: '#6b7280' }}>
              {total > 0 ? `${Math.round((arc.value / total) * 100)}%` : ''}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ScatterViz({
  rows,
  labelKey,
  valueKey,
  width = PREVIEW_WIDTH,
  height = PREVIEW_HEIGHT,
}: {
  rows: Record<string, unknown>[];
  labelKey: string;
  valueKey: string;
  width?: number;
  height?: number;
}) {
  const data = rows.slice(0, 40);
  const xs = data.map((row, i) => i);
  const ys = data.map(row => toNum(row[valueKey]));
  const max = Math.max(1, ...ys);
  const padL = 46;
  const padB = 36;
  const innerW = width - padL - 16;
  const innerH = height - padB - 24;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {[0.25, 0.5, 0.75, 1].map(tick => {
        const y = 24 + innerH * (1 - tick);
        return (
          <g key={tick}>
            <line
              x1={padL}
              x2={width - 8}
              y1={y}
              y2={y}
              stroke="#e5e7eb"
              strokeDasharray="4 4"
            />
            <text x={padL - 6} y={y + 4} textAnchor="end" fontSize={10} fill="#9ca3af">
              {fmtNum(max * tick)}
            </text>
          </g>
        );
      })}
      {data.map((row, i) => {
        const x = padL + (xs[i] / Math.max(1, xs.length - 1)) * innerW;
        const y = 24 + innerH * (1 - ys[i] / max);
        return (
          <g key={i}>
            <circle cx={x} cy={y} r={6} fill={PALETTE[i % PALETTE.length]} opacity={0.75} />
            <title>{`${row[labelKey] ?? ''}: ${fmtNum(row[valueKey])}`}</title>
          </g>
        );
      })}
    </svg>
  );
}

function BigNumberViz({
  label,
  value,
  caption,
}: {
  label: string;
  value: string;
  caption: string;
}) {
  return (
    <StyledBigNumber>
      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
        {caption}
      </Typography.Text>
      <div style={{ fontSize: 52, fontWeight: 700, color: '#4f46e5' }}>{value}</div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Typography.Text>
    </StyledBigNumber>
  );
}

function TableViz({
  columns,
  rows,
}: {
  columns: string[];
  rows: Record<string, unknown>[];
}) {
  return (
    <Table
      size="small"
      pagination={false}
      dataSource={rows.map((row, i) => ({ key: i, ...row }))}
      columns={columns.map(col => ({
        title: col,
        dataIndex: col,
        key: col,
        ellipsis: true,
        render: (value: unknown) => (
          <span style={{ fontSize: 12 }}>{String(value ?? '')}</span>
        ),
      }))}
    />
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const StyledModalBody = styled.div`
  ${({ theme }) => `
    padding: ${theme.sizeUnit * 2}px 0 0;
  `}
`;

const StyledModalHead = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 3}px;
    padding: 0 ${theme.sizeUnit * 3}px ${theme.sizeUnit * 3}px;
    border-bottom: 1px solid ${theme.colorBorderSecondary};
  `}
`;

const StyledVizTile = styled.div`
  ${({ theme }) => `
    width: 44px;
    height: 44px;
    border-radius: ${theme.borderRadius}px;
    background: linear-gradient(135deg, #4f46e5, #7c3aed);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    flex-shrink: 0;
  `}
`;

const StyledGrid = styled.div`
  ${({ theme }) => `
    display: grid;
    grid-template-columns: minmax(0, 1fr) 220px;
    gap: ${theme.sizeUnit * 3}px;
    padding: ${theme.sizeUnit * 3}px;
  `}
`;

const StyledPreviewPane = styled.div`
  ${({ theme }) => `
    min-height: 320px;
    border: 1px solid ${theme.colorBorderSecondary};
    border-radius: ${theme.borderRadius}px;
    background: #fafbfc;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: ${theme.sizeUnit * 2}px;
    overflow: auto;
  `}
`;

const StyledCenter = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 280px;
  width: 100%;
`;

const StyledOptionsPane = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 2}px;
  `}
`;

const StyledOption = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit}px;
  `}
`;

const StyledModalFoot = styled.div`
  ${({ theme }) => `
    display: flex;
    justify-content: flex-end;
    padding: ${theme.sizeUnit * 2}px ${theme.sizeUnit * 3}px;
    border-top: 1px solid ${theme.colorBorderSecondary};
  `}
`;

const StyledBigNumber = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 280px;
`;
