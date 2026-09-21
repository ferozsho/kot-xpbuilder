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
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useHistory, useParams } from 'react-router-dom';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Collapse,
  Input,
  Modal,
  Select,
  Space,
  Loading,
  Tag,
  Typography,
  Upload,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import SubMenu from 'src/features/home/SubMenu';
import {
  createReport,
  fetchDatabases,
  fetchDashboards,
  fetchDatasets,
  fetchReport,
  fetchVizTypes,
  previewReport,
  publishReport,
  unpublishReport,
  updateReport,
  exportPdfUrl,
  exportReportUrl,
  exportXlsxUrl,
  uploadExcel,
} from './api';
import {
  DashboardOption,
  DatasetOption,
  DatabaseOption,
  EMPTY_DEFINITION,
  FieldRef,
  MetricRef,
  PreviewResult,
  PublishResult,
  Report,
  ReportDefinition,
  VizTypeOption,
} from './types';
import DatasetPanel from './components/DatasetPanel';
import FieldsPanel from './components/FieldsPanel';
import FiltersPanel from './components/FiltersPanel';
import RelationshipsPanel from './components/RelationshipsPanel';
import PreviewPanel from './components/PreviewPanel';
import ChartPreviewModal from './components/ChartPreviewModal';

const { Panel } = Collapse;

const StyledLayout = styled.div`
  ${({ theme }) => `
    display: grid;
    /* minmax(0, 1fr) lets the report canvas column shrink below its content
       width, so wide preview tables scroll inside the pane instead of
       stretching the page. */
    grid-template-columns: 340px minmax(0, 1fr);
    gap: ${theme.sizeUnit * 4}px;
    padding: ${theme.sizeUnit * 4}px;
    align-items: start;
  `}
`;

const StyledCanvas = styled.div`
  min-width: 0;
  overflow: hidden;
`;

const StyledLeft = styled.div`
  position: sticky;
  top: ${({ theme }) => theme.sizeUnit * 4}px;
  max-height: calc(100vh - 160px);
  overflow: auto;
`;

const StyledToolbar = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    margin-bottom: ${theme.sizeUnit * 3}px;
    flex-wrap: wrap;
  `}
`;

const StyledMeta = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    flex: 1;
    min-width: 320px;
  `}
`;

// ---- Publish to Superset (professional panel) ---------------------------

const StyledPublish = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 3}px;
  `}
`;

const StyledPublishHead = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    padding: ${theme.sizeUnit * 2}px ${theme.sizeUnit * 3}px;
    border-radius: ${theme.borderRadius}px;
    background: linear-gradient(135deg, #eef2ff, #f5f3ff);
    border: 1px solid #e0e7ff;
  `}
`;

const StyledPublishIcon = styled.div`
  ${({ theme }) => `
    width: 38px;
    height: 38px;
    border-radius: ${theme.borderRadius}px;
    background: linear-gradient(135deg, #4f46e5, #7c3aed);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    flex-shrink: 0;
  `}
`;

const StyledPublishField = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit}px;
  `}
`;

const StyledVizRow = styled.div`
  display: flex;
  gap: 8px;
  align-items: flex-start;

  .publish-viz-select {
    flex: 1;
  }
`;

const StyledPublishActions = styled.div`
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
`;

const StyledPublishedCard = styled.div`
  ${({ theme }) => `
    border: 1px solid #a7f3d0;
    background: linear-gradient(135deg, #ecfdf5, #f0fdf4);
    border-radius: ${theme.borderRadius}px;
    padding: ${theme.sizeUnit * 2}px ${theme.sizeUnit * 3}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 2}px;
  `}
`;

const StyledPublishedRow = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
`;

const StyledPublishHint = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    padding: ${theme.sizeUnit * 2}px;
    border: 1px dashed ${theme.colorBorderSecondary};
    border-radius: ${theme.borderRadius}px;
    color: ${theme.colorTextDescription};
  `}
`;

// Properly-sized, centered loaders (avoids the oversized / clipped floating
// spinner that positions against whatever ancestor happens to be positioned).
const StyledPageLoader = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 55vh;
  padding: 40px;
`;

const StyledInlineLoader = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 220px;
  padding: 32px;
`;

export default function ReportDesignerPage() {
  const history = useHistory();
  const { reportId } = useParams<{ reportId?: string }>();
  const { addDangerToast, addSuccessToast } = useToasts();

  const [datasets, setDatasets] = useState<DatasetOption[]>([]);
  const [datasetsLoaded, setDatasetsLoaded] = useState(false);
  const [loadingReport, setLoadingReport] = useState(Boolean(reportId));

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [definition, setDefinition] =
    useState<ReportDefinition>(EMPTY_DEFINITION);

  const [saving, setSaving] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewResult, setPreviewResult] = useState<PreviewResult | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  // ---- Publish to Superset ----------------------------------------------
  const [report, setReport] = useState<Report | null>(null);
  const [dashboards, setDashboards] = useState<DashboardOption[]>([]);
  const [vizTypes, setVizTypes] = useState<VizTypeOption[]>([]);
  const [publishViz, setPublishViz] = useState<string>('table');
  const [publishChartName, setPublishChartName] = useState('');
  const [publishDashboardId, setPublishDashboardId] = useState<
    number | 'new' | undefined
  >();
  const [publishNewDashboardName, setPublishNewDashboardName] = useState('');
  const [publishing, setPublishing] = useState(false);
  const [unpublishing, setUnpublishing] = useState(false);
  const [publishResult, setPublishResult] = useState<PublishResult | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);

  // ---- Data Modeler (Excel upload) -------------------------------------
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadDatabase, setUploadDatabase] = useState<number | undefined>();
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [databases, setDatabases] = useState<DatabaseOption[]>([]);

  // ---- load datasets -------------------------------------------------
  useEffect(() => {
    fetchDatasets()
      .then(list => setDatasets(list))
      .catch(() => addDangerToast(t('Failed to load datasets')))
      .finally(() => setDatasetsLoaded(true));
  }, [addDangerToast]);

  // ---- load publish options (dashboards + viz types) --------------------
  useEffect(() => {
    fetchDashboards()
      .then(list => setDashboards(list))
      .catch(() => addDangerToast(t('Failed to load dashboards')));
    fetchVizTypes()
      .then(list => {
        setVizTypes(list);
        if (list.length > 0) {
          setPublishViz(list[0].key);
        }
      })
      .catch(() => addDangerToast(t('Failed to load chart types')));
  }, [addDangerToast]);

  // ---- load report definition ----------------------------------------
  useEffect(() => {
    if (!reportId) {
      setLoadingReport(false);
      return;
    }
    setLoadingReport(true);
    fetchReport(Number(reportId))
      .then(reportData => {
        setName(reportData.name);
        setDescription(reportData.description);
        setDefinition({
          ...EMPTY_DEFINITION,
          ...reportData.definition,
        });
        setReport(reportData);
        setPublishChartName(reportData.name);
        if (reportData.viz_type) {
          const match = vizTypes.find(item => item.key === reportData.viz_type);
          if (match) setPublishViz(match.key);
        }
      })
      .catch(() => addDangerToast(t('Failed to load report')))
      .finally(() => setLoadingReport(false));
  }, [reportId, addDangerToast, vizTypes]);

  // ---- definition helpers ----------------------------------------------
  const updateDefinition = useCallback((patch: Partial<ReportDefinition>) => {
    setDefinition(prev => ({ ...prev, ...patch }));
  }, []);

  const handleSelectDataset = useCallback(
    (id: number) => {
      const current = definition.datasets;
      if (current.some(ds => ds.id === id)) return;
      const alias = `d${current.length + 1}`;
      updateDefinition({ datasets: [...current, { id, alias }] });
    },
    [definition.datasets, updateDefinition],
  );

  const handleAddColumn = useCallback(
    (field: FieldRef) => {
      const exists = definition.columns.some(
        item => item.dataset === field.dataset && item.column === field.column,
      );
      if (exists) return;
      updateDefinition({ columns: [...definition.columns, field] });
    },
    [definition.columns, updateDefinition],
  );

  const handleAddGroup = useCallback(
    (field: FieldRef) => {
      const exists = definition.group_by.some(
        item => item.dataset === field.dataset && item.column === field.column,
      );
      if (exists) return;
      updateDefinition({ group_by: [...definition.group_by, field] });
    },
    [definition.group_by, updateDefinition],
  );

  const handleAddMetric = useCallback(
    (metric: MetricRef) => {
      const exists = definition.metrics.some(
        item =>
          item.dataset === metric.dataset &&
          item.column === metric.column &&
          item.aggregate === metric.aggregate,
      );
      if (exists) return;
      updateDefinition({ metrics: [...definition.metrics, metric] });
    },
    [definition.metrics, updateDefinition],
  );

  // ---- order by ---------------------------------------------------------
  const orderByOptions = useMemo(() => {
    const items: Array<{ label: string; value: string }> = [];
    definition.columns.forEach(field => {
      items.push({
        label: `${field.label} (${field.column})`,
        value: `${field.dataset}:${field.column}`,
      });
    });
    definition.metrics.forEach(metric => {
      items.push({
        label: `${metric.label}`,
        value: `${metric.dataset}:${metric.column}:${metric.aggregate}:${metric.label}`,
      });
    });
    return items;
  }, [definition.columns, definition.metrics]);

  const handleAddOrder = (value: string) => {
    const [dataset, column, , ...rest] = value.split(':');
    const label = rest.length > 0 ? rest.join(':') : undefined;
    const entry = {
      dataset: Number(dataset),
      column,
      desc: false,
      ...(label ? { label } : {}),
    };
    const exists = definition.order_by.some(
      item =>
        item.dataset === entry.dataset && item.column === entry.column,
    );
    if (exists) return;
    updateDefinition({ order_by: [...definition.order_by, entry] });
  };

  // ---- save / preview ----------------------------------------------------
  const handleSave = () => {
    setSaving(true);
    const definitionJson: ReportDefinition = {
      ...definition,
      datasets: definition.datasets.map((ds, index) => ({
        ...ds,
        alias: ds.alias || `d${index + 1}`,
      })),
    };
    const action = reportId
      ? updateReport(Number(reportId), name, description, definitionJson)
      : createReport(name, description, definitionJson);
    action
      .then(saved => {
        addSuccessToast(t('Report saved'));
        setReport(saved);
        if (!reportId) {
          history.replace(`/reportdesigner/designer/${saved.id}/`);
        }
      })
      .catch(() => addDangerToast(t('Failed to save report')))
      .finally(() => setSaving(false));
  };

  // ---- publish / unpublish ------------------------------------------------
  const handlePublish = () => {
    if (!reportId) {
      addDangerToast(t('Save the report first to publish it.'));
      return;
    }
    setPublishing(true);
    publishReport(Number(reportId), {
      viz_type: publishViz,
      ...(publishChartName ? { chart_name: publishChartName } : {}),
      ...(publishDashboardId === 'new'
        ? { new_dashboard_name: publishNewDashboardName }
        : publishDashboardId
          ? { dashboard_id: publishDashboardId }
          : {}),
    })
      .then(result => {
        setPublishResult(result);
        addSuccessToast(t('Chart published'));
        return fetchReport(Number(reportId));
      })
      .then(updated => {
        setReport(updated);
        return fetchDashboards();
      })
      .then(list => setDashboards(list))
      .catch(err => {
        const message =
          (err as { message?: string })?.message || t('Failed to publish chart');
        addDangerToast(message);
      })
      .finally(() => setPublishing(false));
  };

  const handleUnpublish = () => {
    if (!reportId) return;
    setUnpublishing(true);
    unpublishReport(Number(reportId))
      .then(() => {
        addSuccessToast(t('Report unpublished'));
        setPublishResult(null);
        return fetchReport(Number(reportId));
      })
      .then(updated => setReport(updated))
      .catch(() => addDangerToast(t('Failed to unpublish report')))
      .finally(() => setUnpublishing(false));
  };

  const handlePreview = () => {
    setPreviewLoading(true);
    setPreviewError(null);
    previewReport(definition)
      .then(result => setPreviewResult(result))
      .catch(err => {
        const message =
          (err as { message?: string })?.message || t('Preview failed');
        setPreviewError(message);
        setPreviewResult(null);
      })
      .finally(() => setPreviewLoading(false));
  };

  // ---- Data Modeler handlers ---------------------------------------------
  const openUpload = () => {
    fetchDatabases()
      .then(list => setDatabases(list))
      .catch(() => addDangerToast(t('Failed to load databases')));
    setUploadDatabase(undefined);
    setUploadFile(null);
    setUploadOpen(true);
  };

  const handleUpload = () => {
    if (uploadDatabase === undefined || !uploadFile) return;
    setUploading(true);
    uploadExcel(uploadDatabase, uploadFile)
      .then(result => {
        addSuccessToast(
          `Uploaded ${result.rows} rows as ${result.table_name}`,
        );
        setUploadOpen(false);
        // Refresh datasets so the new table is available in the picker.
        fetchDatasets()
          .then(list => {
            setDatasets(list);
            handleSelectDataset(result.dataset_id);
          })
          .catch(() => addDangerToast(t('Failed to refresh datasets')));
      })
      .catch(err => {
        const message =
          (err as { message?: string })?.message || t('Upload failed');
        addDangerToast(message);
      })
      .finally(() => setUploading(false));
  };

  if (loadingReport) {
    return (
      <StyledPageLoader>
        <Loading position="normal" size="m" />
      </StyledPageLoader>
    );
  }

  return (
    <>
      <SubMenu
        name={t('Report Designer')}
        buttons={[
          {
            name: t('Back to reports'),
            onClick: () => history.push('/reportdesigner/list/'),
            buttonStyle: 'link',
          },
          {
            name: t('Run preview'),
            onClick: handlePreview,
            buttonStyle: 'secondary',
            loading: previewLoading,
            icon: <Icons.PlayCircleOutlined />,
          },
          {
            name: t('Save'),
            onClick: handleSave,
            buttonStyle: 'primary',
            loading: saving,
            icon: <Icons.SaveOutlined />,
          },
        ]}
      />
      <StyledLayout>
        <StyledLeft>
          <DatasetPanel
            datasets={datasets}
            selectedDatasetIds={definition.datasets.map(ds => ds.id)}
            onSelectDataset={handleSelectDataset}
          />
        </StyledLeft>

        <StyledCanvas>
          <StyledToolbar>
            <StyledMeta>
              <Input
                placeholder={t('Report name')}
                value={name}
                onChange={event => setName(event.target.value)}
                style={{ maxWidth: 360 }}
              />
              <Input
                placeholder={t('Description (optional)')}
                value={description}
                onChange={event => setDescription(event.target.value)}
                style={{ maxWidth: 360 }}
              />
            </StyledMeta>
            <Space wrap>
              <Typography.Text>{t('Limit')}</Typography.Text>
              <Select
                value={definition.limit}
                style={{ width: 100 }}
                options={[100, 500, 1000, 5000, 10000].map(value => ({
                  label: String(value),
                  value,
                }))}
                onChange={(value: number) =>
                  updateDefinition({ limit: value })
                }
              />
              {reportId && (
                <>
                  <Button
                    icon={<Icons.DownloadOutlined />}
                    href={exportReportUrl(Number(reportId))}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t('CSV')}
                  </Button>
                  <Button
                    icon={<Icons.FileOutlined />}
                    href={exportXlsxUrl(Number(reportId))}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t('Excel')}
                  </Button>
                  <Button
                    icon={<Icons.ProfileOutlined />}
                    href={exportPdfUrl(Number(reportId))}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t('PDF')}
                  </Button>
                </>
              )}
              <Button
                icon={<Icons.UploadOutlined />}
                onClick={openUpload}
              >
                {t('Upload Excel')}
              </Button>
            </Space>
          </StyledToolbar>

          {!datasetsLoaded ? (
            <StyledInlineLoader>
              <Loading position="normal" size="m" />
            </StyledInlineLoader>
          ) : (
            <>
              {definition.datasets.length === 0 && (
                <Tag color="orange" style={{ marginBottom: 12 }}>
                  {t('Add at least one dataset from the left panel to start.')}
                </Tag>
              )}

              <Collapse
                accordion
                defaultActiveKey={['fields']}
                bordered
              >
                <Panel
                  key="datasets"
                  header={
                    <Space size={4}>
                      <Icons.DatabaseOutlined />
                      {t('Datasets')}
                      <Tag>{definition.datasets.length}</Tag>
                    </Space>
                  }
                >
                  <Space wrap>
                    {definition.datasets.map(ds => {
                      const meta = datasets.find(item => item.id === ds.id);
                      return (
                        <Tag
                          key={ds.id}
                          color="blue"
                          closable
                          onClose={() =>
                            updateDefinition({
                              datasets: definition.datasets.filter(
                                item => item.id !== ds.id,
                              ),
                            })
                          }
                        >
                          {meta?.table_name ?? `#${ds.id}`} ({ds.alias})
                        </Tag>
                      );
                    })}
                  </Space>
                </Panel>

                <Panel
                  key="relationships"
                  header={
                    <Space size={4}>
                      <Icons.LinkOutlined />
                      {t('Relationships')}
                      <Tag>{definition.relationships.length}</Tag>
                    </Space>
                  }
                >
                  <RelationshipsPanel
                    datasets={datasets.filter(ds =>
                      definition.datasets.some(item => item.id === ds.id),
                    )}
                    relationships={definition.relationships}
                    onChange={relationships =>
                      updateDefinition({ relationships })
                    }
                  />
                </Panel>

                <Panel
                  key="fields"
                  header={
                    <Space size={4}>
                      <Icons.Sigma />
                      {t('Fields')}
                      <Tag>{definition.columns.length}</Tag>
                    </Space>
                  }
                >
                  <FieldsPanel
                    datasets={datasets}
                    columns={definition.columns}
                    groupBy={definition.group_by}
                    metrics={definition.metrics}
                    onAddColumn={handleAddColumn}
                    onRemoveColumn={index =>
                      updateDefinition({
                        columns: definition.columns.filter(
                          (_, i) => i !== index,
                        ),
                      })
                    }
                    onUpdateColumnLabel={(index, label) =>
                      updateDefinition({
                        columns: definition.columns.map((field, i) =>
                          i === index ? { ...field, label } : field,
                        ),
                      })
                    }
                    onAddGroup={handleAddGroup}
                    onRemoveGroup={index =>
                      updateDefinition({
                        group_by: definition.group_by.filter(
                          (_, i) => i !== index,
                        ),
                      })
                    }
                    onAddMetric={handleAddMetric}
                    onRemoveMetric={index =>
                      updateDefinition({
                        metrics: definition.metrics.filter(
                          (_, i) => i !== index,
                        ),
                      })
                    }
                    onUpdateMetric={(index, metric) =>
                      updateDefinition({
                        metrics: definition.metrics.map((item, i) =>
                          i === index ? metric : item,
                        ),
                      })
                    }
                  />

                  <div style={{ marginTop: 16 }}>
                    <Space align="center">
                      <Typography.Text strong>
                        {t('Order by')}
                      </Typography.Text>
                      <Select
                        placeholder={t('Add ordering…')}
                        style={{ width: 260 }}
                        showSearch
                        options={orderByOptions}
                        onChange={handleAddOrder}
                      />
                    </Space>
                    <Space wrap style={{ marginTop: 8 }}>
                      {definition.order_by.map((entry, index) => (
                        <Tag
                          key={`${entry.dataset}-${entry.column}-${index}`}
                          color="cyan"
                          closable
                          onClose={() =>
                            updateDefinition({
                              order_by: definition.order_by.filter(
                                (_, i) => i !== index,
                              ),
                            })
                          }
                        >
                          {entry.column}{' '}
                          {entry.desc ? t('desc') : t('asc')}
                        </Tag>
                      ))}
                    </Space>
                  </div>
                </Panel>

                <Panel
                  key="filters"
                  header={
                    <Space size={4}>
                      <Icons.FilterOutlined />
                      {t('Filters')}
                      <Tag>{definition.filters.length}</Tag>
                    </Space>
                  }
                >
                  <FiltersPanel
                    datasets={datasets.filter(ds =>
                      definition.datasets.some(item => item.id === ds.id),
                    )}
                    filters={definition.filters}
                    onChange={filters => updateDefinition({ filters })}
                  />
                </Panel>

                <Panel
                  key="publish"
                  header={
                    <Space size={4}>
                      <Icons.UploadOutlined />
                      {t('Publish to Charts')}
                      {report?.chart_id ? (
                        <Tag color="green">{t('Published')}</Tag>
                      ) : null}
                    </Space>
                  }
                >
                  {!reportId ? (
                    <StyledPublishHint>
                      <Icons.UploadOutlined />
                      {t('Save the report first, then publish it as a chart '
                        + 'attached to a dashboard.')}
                    </StyledPublishHint>
                  ) : (
                    <StyledPublish>
                      <StyledPublishHead>
                        <StyledPublishIcon>
                          <Icons.UploadOutlined />
                        </StyledPublishIcon>
                        <div style={{ flex: 1 }}>
                          <Typography.Text strong style={{ display: 'block' }}>
                            {t('Publish to Charts')}
                          </Typography.Text>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {t('Turn this report into a chart and attach it '
                              + 'to a dashboard.')}
                          </Typography.Text>
                        </div>
                        {report?.chart_id && (
                          <Tag color="green">{t('Published')}</Tag>
                        )}
                      </StyledPublishHead>

                      <StyledPublishField>
                        <Typography.Text strong>{t('Chart name')}</Typography.Text>
                        <Input
                          placeholder={t('Chart name (defaults to report name)')}
                          value={publishChartName}
                          onChange={event =>
                            setPublishChartName(event.target.value)
                          }
                        />
                      </StyledPublishField>

                      <StyledPublishField>
                        <Typography.Text strong>{t('Chart type')}</Typography.Text>
                        <StyledVizRow>
                          <Select
                            className="publish-viz-select"
                            placeholder={t('Select a chart type…')}
                            value={publishViz ?? null}
                            options={vizTypes.map(item => ({
                              label: item.requires_dttm
                                ? `${item.label} ⏱`
                                : item.label,
                              value: item.key,
                            }))}
                            onChange={setPublishViz}
                          />
                          <Button
                            icon={<Icons.EyeOutlined />}
                            onClick={() => setPreviewOpen(true)}
                          >
                            {t('Preview')}
                          </Button>
                        </StyledVizRow>
                        {vizTypes.find(item => item.key === publishViz)
                          ?.requires_dttm && (
                          <Typography.Text
                            type="warning"
                            style={{ fontSize: 12 }}
                          >
                            {t('This chart type needs a date/time column in '
                              + 'the report output.')}
                          </Typography.Text>
                        )}
                      </StyledPublishField>

                      <StyledPublishField>
                        <Typography.Text strong>
                          {t('Attach to dashboard')}
                        </Typography.Text>
                        <Select
                          placeholder={t('Select a dashboard or create one…')}
                          value={publishDashboardId ?? null}
                          options={[
                            ...dashboards.map(dash => ({
                              label: dash.dashboard_title,
                              value: dash.id,
                            })),
                            { label: t('＋ Create new dashboard'), value: 'new' },
                          ]}
                          showSearch
                          onChange={value =>
                            setPublishDashboardId(
                              value === 'new' ? 'new' : Number(value),
                            )
                          }
                        />
                        {publishDashboardId === 'new' && (
                          <Input
                            placeholder={t('New dashboard name')}
                            value={publishNewDashboardName}
                            onChange={event =>
                              setPublishNewDashboardName(event.target.value)
                            }
                          />
                        )}
                      </StyledPublishField>

                      <StyledPublishActions>
                        <Button
                          type="primary"
                          icon={<Icons.UploadOutlined />}
                          loading={publishing}
                          onClick={handlePublish}
                        >
                          {report?.chart_id
                            ? t('Republish chart')
                            : t('Publish chart')}
                        </Button>
                        {report?.chart_id && (
                          <Button
                            danger
                            icon={<Icons.DeleteOutlined />}
                            loading={unpublishing}
                            onClick={handleUnpublish}
                          >
                            {t('Unpublish')}
                          </Button>
                        )}
                      </StyledPublishActions>

                      {(publishResult || report?.chart_id) && (
                        <StyledPublishedCard>
                          <StyledPublishedRow>
                            <Space>
                              <Icons.CheckCircleFilled
                                style={{ color: '#16a34a' }}
                              />
                              <Typography.Text strong>
                                📊{' '}
                                {publishResult?.chart_name || report?.chart_name}
                              </Typography.Text>
                            </Space>
                            <Space wrap>
                              <Button
                                size="small"
                                href={
                                  publishResult?.explore_url ||
                                  `/explore/?slice_id=${report?.chart_id}`
                                }
                                target="_blank"
                                rel="noreferrer"
                              >
                                {t('Open in Explore')}
                              </Button>
                              {(publishResult?.dashboard_url ||
                                report?.dashboard_id) && (
                                <Button
                                  size="small"
                                  href={
                                    publishResult?.dashboard_url ||
                                    `/superset/dashboard/${report?.dashboard_id}/`
                                  }
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  {t('Open dashboard')}
                                </Button>
                              )}
                            </Space>
                          </StyledPublishedRow>
                          <Typography.Text
                            type="secondary"
                            style={{ fontSize: 12 }}
                          >
                            {publishResult?.chart_id != null && (
                              <>
                                {t('Chart')} #
                                {publishResult?.chart_id || report?.chart_id}
                                {' · '}
                              </>
                            )}
                            {t('Dashboard')} #
                            {publishResult?.dashboard_id || report?.dashboard_id}
                            {publishResult?.dashboard_title
                              ? ` · ${publishResult.dashboard_title}`
                              : ''}
                          </Typography.Text>
                        </StyledPublishedCard>
                      )}
                    </StyledPublish>
                  )}
                </Panel>
              </Collapse>

              <PreviewPanel
                result={previewResult}
                loading={previewLoading}
                error={previewError}
                onRefresh={handlePreview}
              />
            </>
          )}
        </StyledCanvas>
      </StyledLayout>

      <Modal
        open={uploadOpen}
        title={t('Upload Excel / CSV as a dataset')}
        onCancel={() => setUploadOpen(false)}
        onOk={handleUpload}
        okText={t('Upload')}
        confirmLoading={uploading}
        okButtonProps={{ disabled: uploadDatabase === undefined || !uploadFile }}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <div>
            <Typography.Text strong>{t('Target database')}</Typography.Text>
            <Select
              placeholder={t('Select a writable database…')}
              options={databases.map(db => ({
                label: db.database_name,
                value: db.id,
              }))}
              value={uploadDatabase ?? null}
              style={{ width: '100%', marginTop: 4 }}
              onChange={setUploadDatabase}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {t('Note: the read replica is read-only — pick a writable '
                + 'connection so the staging table can be created.')}
            </Typography.Text>
          </div>
          <Upload
            beforeUpload={file => {
              setUploadFile(file);
              return false;
            }}
            onRemove={() => setUploadFile(null)}
            maxCount={1}
            accept=".xlsx,.xls,.csv"
          >
            <Button icon={<Icons.UploadOutlined />}>
              {t('Choose file')}
            </Button>
          </Upload>
        </Space>
      </Modal>

      <ChartPreviewModal
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
        onPublish={handlePublish}
        definition={definition}
        vizKey={publishViz}
        vizLabel={
          vizTypes.find(item => item.key === publishViz)?.label ?? publishViz
        }
        chartName={publishChartName || name}
        publishing={publishing}
      />
    </>
  );
}
