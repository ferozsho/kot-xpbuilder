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
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Card,
  Space,
  Tag,
  Tooltip,
} from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import {
  archiveConversation,
  createConversation,
  describeError,
  fetchConversation,
  fetchConversations,
  sendMessage,
} from './api';
import { ConversationRecord, MessageRecord } from './types';

const StyledWrapper = styled.div`
  ${({ theme }) => `
    padding: ${theme.sizeUnit * 4}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 3}px;
  `}
`;

const StyledHeader = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: ${theme.sizeUnit * 3}px;
  `}
`;

const StyledTitle = styled.h2`
  ${({ theme }) => `
    margin: 0;
    font-size: ${theme.fontSizeHeading3}px;
    font-weight: ${theme.fontWeightStrong};
  `}
`;

const StyledSubtitle = styled.p`
  ${({ theme }) => `
    margin: ${theme.sizeUnit}px 0 0;
    color: ${theme.colorTextSecondary};
  `}
`;

const StyledLayout = styled.div`
  ${({ theme }) => `
    display: grid;
    grid-template-columns: minmax(220px, 320px) minmax(0, 1fr);
    gap: ${theme.sizeUnit * 3}px;
    align-items: start;

    @media (max-width: 900px) {
      grid-template-columns: minmax(0, 1fr);
    }
  `}
`;

const StyledList = styled.ul`
  ${({ theme }) => `
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit}px;
  `}
`;

const StyledListItem = styled.li<{ $active: boolean }>`
  ${({ theme, $active }) => `
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: ${theme.sizeUnit}px;
    padding: ${theme.sizeUnit * 2}px;
    border-radius: ${theme.borderRadius}px;
    cursor: pointer;
    background: ${$active ? theme.colorPrimaryBg : 'transparent'};

    &:hover {
      background: ${$active ? theme.colorPrimaryBg : theme.colorFillTertiary};
    }
  `}
`;

const StyledConversationTitle = styled.span`
  ${() => `
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  `}
`;

const StyledThread = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 3}px;
    max-height: 52vh;
    overflow-y: auto;
    padding-right: ${theme.sizeUnit}px;
  `}
`;

const StyledBubble = styled.div<{ $role: 'user' | 'assistant' }>`
  ${({ theme, $role }) => `
    align-self: ${$role === 'user' ? 'flex-end' : 'flex-start'};
    max-width: 80%;
    padding: ${theme.sizeUnit * 2}px ${theme.sizeUnit * 3}px;
    border-radius: ${theme.borderRadius}px;
    background: ${$role === 'user' ? theme.colorPrimaryBg : theme.colorFillTertiary};
    white-space: pre-wrap;
    word-break: break-word;
  `}
`;

const StyledMeta = styled.div`
  ${({ theme }) => `
    margin-top: ${theme.sizeUnit}px;
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const StyledComposer = styled.div`
  ${({ theme }) => `
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 3}px;
  `}
`;

const StyledTextarea = styled.textarea`
  ${({ theme }) => `
    width: 100%;
    min-height: ${theme.sizeUnit * 20}px;
    resize: vertical;
    padding: ${theme.sizeUnit * 2}px;
    border: 1px solid ${theme.colorBorder};
    border-radius: ${theme.borderRadius}px;
    font-family: inherit;
    font-size: inherit;
  `}
`;

const StyledNotice = styled.div`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
    padding: ${theme.sizeUnit * 4}px 0;
    text-align: center;
  `}
`;

const isAssistantError = (message: MessageRecord) =>
  message.structured_content?.kind === 'error';

const isClarification = (message: MessageRecord) =>
  message.structured_content?.kind === 'clarification';

const intentLabel = (intent?: string | null) =>
  intent ? t(intent.charAt(0).toUpperCase() + intent.slice(1)) : '';

export default function AIPromptPage() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [conversations, setConversations] = useState<ConversationRecord[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [draft, setDraft] = useState('');
  const [loadingList, setLoadingList] = useState(true);
  const [loadingThread, setLoadingThread] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const threadEnd = useRef<HTMLDivElement | null>(null);

  const loadList = useCallback(async () => {
    setLoadingList(true);
    try {
      const response = await fetchConversations();
      setConversations(response.result ?? []);
      setError(null);
    } catch (caught) {
      setError(await describeError(caught));
    } finally {
      setLoadingList(false);
    }
  }, []);

  const openConversation = useCallback(async (id: string) => {
    setActiveId(id);
    setLoadingThread(true);
    try {
      const detail = await fetchConversation(id);
      setMessages(detail.messages ?? []);
      setError(null);
    } catch (caught) {
      setError(await describeError(caught));
      setMessages([]);
    } finally {
      setLoadingThread(false);
    }
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: 'end' });
  }, [messages, sending]);

  const startConversation = useCallback(async () => {
    try {
      const created = await createConversation();
      setConversations(current => [created, ...current]);
      setActiveId(created.id);
      setMessages([]);
      setError(null);
    } catch (caught) {
      addDangerToast(await describeError(caught));
    }
  }, [addDangerToast]);

  const archive = useCallback(
    async (id: string) => {
      try {
        await archiveConversation(id);
        addSuccessToast(t('Conversation archived'));
        setConversations(current => current.filter(item => item.id !== id));
        if (activeId === id) {
          setActiveId(null);
          setMessages([]);
        }
      } catch (caught) {
        addDangerToast(await describeError(caught));
      }
    },
    [activeId, addDangerToast, addSuccessToast],
  );

  const submit = useCallback(async () => {
    const content = draft.trim();
    if (!content || sending) {
      return;
    }
    let conversationId = activeId;
    setSending(true);
    setError(null);
    try {
      if (!conversationId) {
        const created = await createConversation();
        setConversations(current => [created, ...current]);
        setActiveId(created.id);
        conversationId = created.id;
      }
      // Show the question immediately; the server is the source of truth once
      // the turn returns.
      setMessages(current => [
        ...current,
        {
          id: `pending-${Date.now()}`,
          conversation_id: conversationId as string,
          role: 'user',
          content,
          structured_content: {},
          created_at: null,
        },
      ]);
      setDraft('');
      const turn = await sendMessage(conversationId, content);
      const detail = await fetchConversation(conversationId);
      setMessages(detail.messages ?? []);
      setConversations(current =>
        current.map(item =>
          item.id === conversationId
            ? { ...item, last_message_at: new Date().toISOString() }
            : item,
        ),
      );
      addSuccessToast(
        turn.clarification
          ? t('XP Prompt needs one more detail')
          : `${t('Answered by')} ${turn.provider} · ${turn.model}`,
      );
    } catch (caught) {
      setError(await describeError(caught));
      if (conversationId) {
        // Keep the stored transcript authoritative after a failed turn.
        try {
          const detail = await fetchConversation(conversationId);
          setMessages(detail.messages ?? []);
        } catch {
          // the toast below already explains the failure
        }
      }
    } finally {
      setSending(false);
    }
  }, [activeId, addSuccessToast, draft, sending]);

  const sortedConversations = useMemo(
    () =>
      [...conversations].sort((left, right) =>
        (right.last_message_at ?? right.created_at ?? '').localeCompare(
          left.last_message_at ?? left.created_at ?? '',
        ),
      ),
    [conversations],
  );

  return (
    <StyledWrapper>
      <StyledHeader>
        <div>
          <StyledTitle>{t('XP Prompt')}</StyledTitle>
          <StyledSubtitle>
            {t(
              'Ask questions about your data. Answers are stored in your own ' +
                'conversations and are never shared with other users.',
            )}
          </StyledSubtitle>
        </div>
        <Space>
          <Button buttonStyle="secondary" onClick={loadList} loading={loadingList}>
            {t('Refresh')}
          </Button>
          <Button buttonStyle="primary" onClick={startConversation}>
            {t('New chat')}
          </Button>
        </Space>
      </StyledHeader>

      {error && (
        <Card>
          <StyledNotice>{error}</StyledNotice>
        </Card>
      )}

      <StyledLayout>
        <Card title={t('Conversations')}>
          {loadingList && <StyledNotice>{t('Loading…')}</StyledNotice>}
          {!loadingList && sortedConversations.length === 0 && (
            <StyledNotice>{t('No conversations yet.')}</StyledNotice>
          )}
          <StyledList>
            {sortedConversations.map(conversation => (
              <StyledListItem
                key={conversation.id}
                $active={conversation.id === activeId}
                onClick={() => openConversation(conversation.id)}
                role="button"
                tabIndex={0}
                onKeyDown={event => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    openConversation(conversation.id);
                  }
                }}
              >
                <StyledConversationTitle>{conversation.title}</StyledConversationTitle>
                <Tooltip title={t('Archive conversation')}>
                  <Button
                    buttonSize="xsmall"
                    buttonStyle="tertiary"
                    onClick={event => {
                      event.stopPropagation();
                      archive(conversation.id);
                    }}
                  >
                    {t('Archive')}
                  </Button>
                </Tooltip>
              </StyledListItem>
            ))}
          </StyledList>
        </Card>

        <Card title={t('Chat')}>
          {!activeId && (
            <StyledNotice>
              {t('Start a new chat or pick a conversation on the left.')}
            </StyledNotice>
          )}
          {activeId && loadingThread && (
            <StyledNotice>{t('Loading conversation…')}</StyledNotice>
          )}
          {activeId && !loadingThread && messages.length === 0 && (
            <StyledNotice>
              {t('Ask the first question to begin this conversation.')}
            </StyledNotice>
          )}
          {activeId && !loadingThread && messages.length > 0 && (
            <StyledThread>
              {messages.map(message => (
                <StyledBubble
                  key={message.id}
                  $role={message.role === 'user' ? 'user' : 'assistant'}
                >
                  {isAssistantError(message) && (
                    <Tag color="red">{t('Provider error')}</Tag>
                  )}
                  {isClarification(message) && (
                    <Tag color="gold">{t('Needs clarification')}</Tag>
                  )}
                  <div>{message.content ?? t('(content unavailable)')}</div>
                  {message.role === 'assistant' &&
                    message.structured_content?.model && (
                      <StyledMeta>
                        {message.structured_content.intent
                          ? `${intentLabel(message.structured_content.intent)} · `
                          : ''}
                        {message.structured_content.provider}{' '}
                        {message.structured_content.model}
                        {message.structured_content.usage?.total_tokens
                          ? ` · ${message.structured_content.usage.total_tokens} ${t('tokens')}`
                          : ''}
                        {message.structured_content.correlation_id
                          ? ` · ${message.structured_content.correlation_id.slice(0, 8)}`
                          : ''}
                      </StyledMeta>
                    )}
                </StyledBubble>
              ))}
              <div ref={threadEnd} />
            </StyledThread>
          )}

          <StyledComposer>
            <StyledTextarea
              value={draft}
              placeholder={t('Ask about your data…')}
              onChange={event => setDraft(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                  submit();
                }
              }}
              aria-label={t('Message')}
            />
            <Space>
              <Button
                buttonStyle="primary"
                loading={sending}
                disabled={!draft.trim()}
                onClick={submit}
              >
                {t('Send')}
              </Button>
              <StyledMeta>
                {t('Ctrl/Cmd + Enter to send. Answers use the default AI provider.')}
              </StyledMeta>
            </Space>
          </StyledComposer>
        </Card>
      </StyledLayout>
    </StyledWrapper>
  );
}
