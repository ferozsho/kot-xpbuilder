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
import { useHistory } from 'react-router-dom';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Table, Tag, Popconfirm, Space } from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import SubMenu from 'src/features/home/SubMenu';
import {
  deleteReport,
  fetchReports,
  exportPdfUrl,
  exportReportUrl,
  exportXlsxUrl,
} from './api';
import { Report } from './types';

const StyledWrapper = styled.div`
  width: 100%;
  padding: ${({ theme }) => theme.sizeUnit * 6}px;
  max-width: 1400px;
  margin: 0 auto;

  .ant-table-wrapper {
    background: ${({ theme }) => theme.colorFillAlter};
    border-radius: ${({ theme }) => theme.borderRadius}px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
  }

  .ant-table-thead > tr > th {
    font-weight: ${({ theme }) => theme.fontWeightStrong};
    background: ${({ theme }) => theme.colorFillSecondary};
  }

  .ant-table-tbody > tr > td {
    vertical-align: middle;
  }
`;

const StyledActions = styled.div`
  display: inline-flex;
  align-items: center;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  white-space: nowrap;
`;

export default function ReportListPage() {
  const history = useHistory();
  const { addDangerToast, addSuccessToast } = useToasts();
  const [reports, setReports] = useState<Report[]>([]);
  const [loading, setLoading] = useState(true);

  const loadReports = useCallback(() => {
    setLoading(true);
    fetchReports()
      .then(list => setReports(list))
      .catch(() => addDangerToast(t('Failed to load reports')))
      .finally(() => setLoading(false));
  }, [addDangerToast]);

  useEffect(() => {
    loadReports();
  }, [loadReports]);

  const handleDelete = (report: Report) => {
    deleteReport(report.id)
      .then(() => {
        addSuccessToast(t('Report deleted'));
        loadReports();
      })
      .catch(() => addDangerToast(t('Failed to delete report')));
  };

  const columns = useMemo(
    () => [
      {
        title: t('Name'),
        dataIndex: 'name',
        key: 'name',
        width: 250,
        ellipsis: true,
        render: (name: string, report: Report) => (
          <a
            href={`/reportdesigner/designer/${report.id}/`}
            onClick={ev => {
              ev.preventDefault();
              history.push(`/reportdesigner/designer/${report.id}/`);
            }}
          >
            {name}
          </a>
        ),
      },
      {
        title: t('Datasets'),
        key: 'datasets',
        width: 200,
        render: (_: unknown, report: Report) => (
          <Space wrap>
            {report.definition.datasets.map(ds => (
              <Tag key={ds.id} color="blue">
                {ds.alias || `d${ds.id}`}
              </Tag>
            ))}
          </Space>
        ),
      },
      {
        title: t('Published'),
        key: 'published',
        width: 240,
        render: (_: unknown, report: Report) => {
          if (!report.chart_id) return '—';
          return (
            <Space wrap>
              <Tag color="green">📊 {report.chart_name || t('Published')}</Tag>
              {report.chart_id != null && (
                <a
                  href={`/explore/?slice_id=${report.chart_id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t('Chart')} #{report.chart_id}
                </a>
              )}
              {report.dashboard_id != null && (
                <a
                  href={`/superset/dashboard/${report.dashboard_id}/`}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t('Dashboard')} #{report.dashboard_id}
                </a>
              )}
            </Space>
          );
        },
      },
      {
        title: t('Changed'),
        dataIndex: 'changed_on',
        key: 'changed_on',
        width: 180,
        render: (value?: string | null) =>
          value ? new Date(value).toLocaleString() : '—',
      },
      {
        title: t('Actions'),
        key: 'actions',
        width: 260,
        render: (_: unknown, report: Report) => (
          <StyledActions>
            <Button
              type="primary"
              ghost
              icon={<Icons.EditOutlined />}
              onClick={() =>
                history.push(`/reportdesigner/designer/${report.id}/`)
              }
            >
              {t('Open')}
            </Button>
            <Button
              icon={<Icons.DownloadOutlined />}
              href={exportReportUrl(report.id)}
              target="_blank"
              rel="noreferrer"
              title={t('Export CSV')}
            />
            <Button
              icon={<Icons.FileOutlined />}
              href={exportXlsxUrl(report.id)}
              target="_blank"
              rel="noreferrer"
              title={t('Export Excel')}
            />
            <Button
              icon={<Icons.ProfileOutlined />}
              href={exportPdfUrl(report.id)}
              target="_blank"
              rel="noreferrer"
              title={t('Export PDF')}
            />
            <Popconfirm
              title={t('Delete this report?')}
              onConfirm={() => handleDelete(report)}
            >
              <Button danger icon={<Icons.DeleteOutlined />} title={t('Delete')} />
            </Popconfirm>
          </StyledActions>
        ),
      },
    ],
    [history, addDangerToast],
  );

  return (
    <>
      <SubMenu
        name={t('Report Designer')}
        buttons={[
          {
            name: t('Create report'),
            onClick: () => history.push('/reportdesigner/designer/'),
            buttonStyle: 'primary',
            icon: <Icons.PlusOutlined />,
          },
        ]}
      />
      <StyledWrapper>
        <Table<Report>
          rowKey="id"
          dataSource={reports}
          columns={columns}
          loading={loading}
          scroll={{ x: 'max-content' }}
          pagination={{ pageSize: 20 }}
        />
      </StyledWrapper>
    </>
  );
}

