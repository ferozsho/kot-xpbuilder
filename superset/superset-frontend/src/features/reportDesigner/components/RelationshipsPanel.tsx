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
import { useMemo, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Select,
  Space,
  Tag,
  Typography,
  Divider,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { DatasetOption, JOIN_TYPES, Relationship } from '../types';

const StyledRow = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit}px;
    padding: ${theme.sizeUnit}px 0;
    border-bottom: 1px solid ${theme.colorBorderSecondary};
  `}
`;

type Props = {
  datasets: DatasetOption[];
  relationships: Relationship[];
  onChange: (relationships: Relationship[]) => void;
};

function datasetName(datasets: DatasetOption[], id: number): string {
  const ds = datasets.find(item => item.id === id);
  return ds ? ds.table_name : `#${id}`;
}

export default function RelationshipsPanel({
  datasets,
  relationships,
  onChange,
}: Props) {
  const [leftDataset, setLeftDataset] = useState<number | undefined>();
  const [leftColumn, setLeftColumn] = useState<string | undefined>();
  const [rightDataset, setRightDataset] = useState<number | undefined>();
  const [rightColumn, setRightColumn] = useState<string | undefined>();
  const [joinType, setJoinType] = useState<string>('INNER');

  const datasetOptions = useMemo(
    () => datasets.map(ds => ({ label: ds.table_name, value: ds.id })),
    [datasets],
  );

  const leftColumns = useMemo(
    () =>
      datasets
        .find(ds => ds.id === leftDataset)
        ?.columns.map(col => ({ label: col.column_name, value: col.column_name })) ?? [],
    [datasets, leftDataset],
  );

  const rightColumns = useMemo(
    () =>
      datasets
        .find(ds => ds.id === rightDataset)
        ?.columns.map(col => ({ label: col.column_name, value: col.column_name })) ?? [],
    [datasets, rightDataset],
  );

  const canAdd =
    leftDataset !== undefined &&
    leftColumn !== undefined &&
    rightDataset !== undefined &&
    rightColumn !== undefined &&
    !(leftDataset === rightDataset && leftColumn === rightColumn);

  const handleAdd = () => {
    if (
      leftDataset === undefined ||
      leftColumn === undefined ||
      rightDataset === undefined ||
      rightColumn === undefined
    ) {
      return;
    }
    const rel: Relationship = {
      left_dataset: leftDataset,
      left_column: leftColumn,
      right_dataset: rightDataset,
      right_column: rightColumn,
      join_type: joinType as Relationship['join_type'],
    };
    onChange([...relationships, rel]);
    setLeftColumn(undefined);
    setRightColumn(undefined);
  };

  const handleRemove = (index: number) => {
    onChange(relationships.filter((_, i) => i !== index));
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {relationships.length === 0 && (
        <Typography.Text type="secondary">
          {t('Define how the tables relate to each other (JOINs).')}
        </Typography.Text>
      )}
      {relationships.map((rel, index) => (
        <StyledRow key={`${index}-${rel.left_dataset}-${rel.left_column}`}>
          <Tag color="green">{datasetName(datasets, rel.left_dataset)}</Tag>
          <Typography.Text code>{rel.left_column}</Typography.Text>
          <Icons.ArrowRightOutlined />
          <Tag color="green">{datasetName(datasets, rel.right_dataset)}</Tag>
          <Typography.Text code>{rel.right_column}</Typography.Text>
          <Tag color="purple">{rel.join_type} JOIN</Tag>
          <Button
            type="text"
            danger
            size="small"
            icon={<Icons.DeleteOutlined />}
            onClick={() => handleRemove(index)}
          />
        </StyledRow>
      ))}

      <Divider style={{ margin: '4px 0' }} />

      <Space wrap>
        <Select
          placeholder={t('Left table')}
          options={datasetOptions}
          value={leftDataset ?? null}
          style={{ width: 160 }}
          onChange={setLeftDataset}
        />
        <Select
          placeholder={t('Left column')}
          options={leftColumns}
          value={leftColumn ?? null}
          style={{ width: 140 }}
          showSearch
          onChange={setLeftColumn}
        />
        <Select
          placeholder={t('Join')}
          options={JOIN_TYPES.map(jt => ({ label: jt, value: jt }))}
          value={joinType}
          style={{ width: 100 }}
          onChange={setJoinType}
        />
        <Select
          placeholder={t('Right table')}
          options={datasetOptions}
          value={rightDataset ?? null}
          style={{ width: 160 }}
          onChange={setRightDataset}
        />
        <Select
          placeholder={t('Right column')}
          options={rightColumns}
          value={rightColumn ?? null}
          style={{ width: 140 }}
          showSearch
          onChange={setRightColumn}
        />
        <Button
          type="primary"
          icon={<Icons.PlusOutlined />}
          disabled={!canAdd}
          onClick={handleAdd}
        >
          {t('Add relationship')}
        </Button>
      </Space>
    </Space>
  );
}
