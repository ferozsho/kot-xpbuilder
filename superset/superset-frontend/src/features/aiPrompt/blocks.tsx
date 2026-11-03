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
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Tag } from '@superset-ui/core/components';
import { ResponseBlock } from './types';

/**
 * Renders the structured response blocks of one assistant message.
 *
 * The backend normalizes every block (unknown types dropped, strings truncated,
 * previews capped), so this component only has to be readable: each block gets a
 * visible label, lists/tables are real HTML for screen readers, and a preview
 * that was cut short says so instead of pretending to be complete.
 */

const StyledBlocks = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 2}px;
  `}
`;

const StyledBlock = styled.section`
  ${({ theme }) => `
    border-left: 3px solid ${theme.colorBorder};
    padding-left: ${theme.sizeUnit * 2}px;
  `}
`;

const StyledBlockTitle = styled.h4`
  ${({ theme }) => `
    margin: 0 0 ${theme.sizeUnit}px;
    font-size: ${theme.fontSizeSM}px;
    font-weight: ${theme.fontWeightStrong};
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: ${theme.colorTextSecondary};
  `}
`;

const StyledList = styled.ul`
  ${({ theme }) => `
    margin: 0;
    padding-left: ${theme.sizeUnit * 4}px;
  `}
`;

const StyledOrderedList = styled.ol`
  ${({ theme }) => `
    margin: 0;
    padding-left: ${theme.sizeUnit * 4}px;
  `}
`;

const StyledWarning = styled(StyledBlock)`
  ${({ theme }) => `
    border-left-color: ${theme.colorWarning};
  `}
`;

const StyledQuestion = styled.p`
  ${({ theme }) => `
    margin: 0;
    font-weight: ${theme.fontWeightStrong};
  `}
`;

const StyledSql = styled.pre`
  ${({ theme }) => `
    margin: 0;
    padding: ${theme.sizeUnit * 2}px;
    overflow-x: auto;
    background: ${theme.colorFillTertiary};
    border-radius: ${theme.borderRadius}px;
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const StyledTable = styled.table`
  ${({ theme }) => `
    border-collapse: collapse;
    font-size: ${theme.fontSizeSM}px;

    th, td {
      border: 1px solid ${theme.colorBorder};
      padding: ${theme.sizeUnit}px ${theme.sizeUnit * 2}px;
      text-align: left;
      vertical-align: top;
    }
  `}
`;

const StyledMeta = styled.p`
  ${({ theme }) => `
    margin: ${theme.sizeUnit}px 0 0;
    font-size: ${theme.fontSizeSM}px;
    color: ${theme.colorTextSecondary};
  `}
`;

const StyledLinks = styled.ul`
  ${({ theme }) => `
    margin: 0;
    padding-left: ${theme.sizeUnit * 4}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit}px;
  `}
`;

/**
 * Native routes, copied from `src/views/routes.tsx` so a link always points at a
 * page this build actually serves. The backend stores only the object identity.
 */
const ENTITY_PATHS: Record<string, (id: string) => string> = {
  chart: id => `/explore/?slice_id=${encodeURIComponent(id)}`,
  dashboard: id => `/xpbuilder/dashboard/${encodeURIComponent(id)}/`,
  dataset: id => `/dataset/${encodeURIComponent(id)}`,
};

function entityHref(entityType: string, entityId: string): string | null {
  const build = ENTITY_PATHS[entityType];
  return build ? build(entityId) : null;
}

function BlockFrame({
  title,
  children,
  warning = false,
}: {
  title: string;
  children: React.ReactNode;
  warning?: boolean;
}) {
  const Wrapper = warning ? StyledWarning : StyledBlock;
  return (
    <Wrapper>
      <StyledBlockTitle>{title}</StyledBlockTitle>
      {children}
    </Wrapper>
  );
}

function Block({ block }: { block: ResponseBlock }) {
  switch (block.type) {
    case 'clarification':
      return (
        <BlockFrame title={t('Needs one detail')}>
          <StyledQuestion>{block.question}</StyledQuestion>
        </BlockFrame>
      );
    case 'plan':
      return (
        <BlockFrame title={block.title || t('Plan')}>
          <StyledOrderedList>
            {block.items.map(item => (
              <li key={item}>{item}</li>
            ))}
          </StyledOrderedList>
        </BlockFrame>
      );
    case 'assumptions':
      return (
        <BlockFrame title={t('Assumptions')}>
          <StyledList>
            {block.items.map(item => (
              <li key={item}>{item}</li>
            ))}
          </StyledList>
        </BlockFrame>
      );
    case 'warnings':
      return (
        <BlockFrame title={t('Warnings')} warning>
          <StyledList>
            {block.items.map(item => (
              <li key={item}>{item}</li>
            ))}
          </StyledList>
        </BlockFrame>
      );
    case 'sql_preview':
      return (
        <BlockFrame title={t('SQL preview')}>
          <StyledSql>
            <code>{block.sql}</code>
          </StyledSql>
          <StyledMeta>
            {block.dialect ? `${block.dialect} · ` : ''}
            {block.truncated ? t('Truncated for preview') : t('Preview only')}
          </StyledMeta>
        </BlockFrame>
      );
    case 'result_preview':
      return (
        <BlockFrame title={t('Result preview')}>
          <StyledTable aria-label={t('Result preview')}>
            <thead>
              <tr>
                {block.columns.map(column => (
                  <th key={column} scope="col">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`row-${rowIndex}`}>
                  {row.map((cell, cellIndex) => (
                    <td key={`cell-${rowIndex}-${cellIndex}`}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </StyledTable>
          <StyledMeta>
            {block.truncated
              ? t('Showing a preview of %(total)s rows', {
                  total: block.row_count,
                })
              : t('%(total)s rows', { total: block.row_count })}
          </StyledMeta>
        </BlockFrame>
      );
    case 'dataset_profile':
      return (
        <BlockFrame title={t('Dataset profile')}>
          <StyledTable aria-label={t('Dataset profile')}>
            <thead>
              <tr>
                <th scope="col">{t('Column')}</th>
                <th scope="col">{t('Type')}</th>
                <th scope="col">{t('Nullable')}</th>
              </tr>
            </thead>
            <tbody>
              {block.columns.map(column => (
                <tr key={column.name}>
                  <td>{column.name}</td>
                  <td>{column.type}</td>
                  <td>{column.nullable ? t('yes') : t('no')}</td>
                </tr>
              ))}
            </tbody>
          </StyledTable>
          {block.table && <StyledMeta>{block.table}</StyledMeta>}
        </BlockFrame>
      );
    case 'chart_recommendation':
      return (
        <BlockFrame title={t('Chart recommendation')}>
          <Tag color="blue">{block.chart_type}</Tag>
          <StyledMeta>
            {block.x ? `${t('Group by')} ${block.x}` : ''}
            {block.y.length ? ` · ${t('Measures')}: ${block.y.join(', ')}` : ''}
          </StyledMeta>
          {block.reason && <StyledMeta>{block.reason}</StyledMeta>}
        </BlockFrame>
      );
    case 'dashboard_proposal':
      return (
        <BlockFrame title={block.title || t('Dashboard proposal')}>
          <StyledList>
            {block.sections.map(section => (
              <li key={section.title}>
                {section.title}
                {section.items.length > 0 && (
                  <StyledList>
                    {section.items.map(item => (
                      <li key={item}>{item}</li>
                    ))}
                  </StyledList>
                )}
              </li>
            ))}
          </StyledList>
        </BlockFrame>
      );
    case 'repair':
      return (
        <BlockFrame title={t('Repair')}>
          <Tag color={block.outcome === 'fixed' ? 'green' : 'orange'}>
            {block.outcome}
          </Tag>
          <StyledMeta>
            {t('%(count)s attempt(s)', { count: block.attempts })}
            {block.notes ? ` · ${block.notes}` : ''}
          </StyledMeta>
        </BlockFrame>
      );
    case 'links':
      return (
        <BlockFrame title={t('Open in Superset')}>
          <StyledLinks>
            {block.items.map(item => {
              const href = entityHref(item.entity_type, item.entity_id);
              return href ? (
                <li key={`${item.entity_type}-${item.entity_id}`}>
                  <a href={href}>{item.label}</a>
                </li>
              ) : null;
            })}
          </StyledLinks>
        </BlockFrame>
      );
    default:
      // The server only sends known types; a stale page must not crash on a
      // newer one.
      return null;
  }
}

export default function ResponseBlocks({ blocks }: { blocks?: ResponseBlock[] }) {
  if (!blocks || blocks.length === 0) {
    return null;
  }
  return (
    <StyledBlocks>
      {blocks.map((block, index) => (
        <Block key={`${block.type}-${index}`} block={block} />
      ))}
    </StyledBlocks>
  );
}
