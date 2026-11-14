import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { keys } from '../keys';
import type {
  MailFormLinkBody,
  MailList,
} from '../types';
import { pollUnlessNotServed } from './shared';

export const useMailboxes = (org: string) => useQuery({ queryKey: keys.mailboxes(org), queryFn: () => fleet.mailboxes(org), refetchInterval: pollUnlessNotServed(60_000) });
export const useMailMessages = (org: string) => useQuery({ queryKey: keys.mailMessages(org), queryFn: () => fleet.mailMessages(org) });
/** One message in full. The list carries only a clip, so the reader asks for the body when it opens. */
export const useMailMessage = (org: string, id: string) =>
  useQuery({ queryKey: [...keys.mailMessages(org), id], queryFn: () => fleet.mailMessage(org, id), enabled: Boolean(id) });
export const useLeadBatches = (org: string) => useQuery({ queryKey: keys.leadBatches(org), queryFn: () => fleet.leadBatches(org) });
export const useLeadBatch = (org: string, id: string) => useQuery({ queryKey: keys.leadBatch(org, id), queryFn: () => fleet.leadBatch(org, id) });
export const useMailLists = (org: string) => useQuery({ queryKey: keys.mailLists(org), queryFn: () => fleet.mailLists(org) });
export const useMailList = (org: string, id: string) => useQuery({ queryKey: keys.mailList(org, id), queryFn: () => fleet.mailList(org, id) });
export const useMailRoutine = (org: string, id: string) => useQuery({ queryKey: keys.mailRoutine(org, id), queryFn: () => fleet.mailRoutine(org, id) });
export const useMailDiscover = (org: string) => useMutation({ mutationFn: (address: string) => fleet.mailDiscover(org, address), meta: { inline: true } });

export function useMailMark(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, read }: { id: string; read: boolean }) => fleet.mailMark(org, id, read),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.mailMessages(org) }),
  });
}

/** The link to the node's own form for a new mailbox. Only the link comes back, never a field. */
export function useMailFormLink(org: string) {
  return useMutation({ mutationFn: (body: MailFormLinkBody) => fleet.mailFormLink(org, body), meta: { inline: true } });
}

/** Whether the node's form is still open, asked every two seconds until it has ended. */
export function useMailFormState(org: string, token: string | null) {
  return useQuery({
    queryKey: keys.mailForm(org, token ?? ''),
    queryFn: () => fleet.mailFormState(org, token ?? ''),
    enabled: Boolean(token),
    refetchInterval: (query) => (query.state.data?.state === 'open' ? 2_000 : false),
  });
}

/** Cancels the node's form for a link: it ends at once, and the password it would have taken is never asked for. */
export function useEndMailForm(org: string) {
  return useMutation({ mutationFn: (token: string) => fleet.mailFormEnd(org, token), meta: { inline: true } });
}

export function useMailSend(org: string) {
  return useMutation({ mutationFn: (body: { mailbox: string; to: string; subject: string; body: string; reply_to?: string }) => fleet.mailSend(org, body), meta: { inline: true } });
}

/** Keeps the ones a person left in, or none. Either way the batch is decided, once. */
export function useDecideBatch(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (keep: string[]) => fleet.decideLeadBatch(org, id, keep),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.leadBatches(org) });
      void client.invalidateQueries({ queryKey: keys.mailLists(org) });
    },
  });
}

export function useUpdateMailList(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { state?: MailList['state']; pace?: number }) => fleet.updateMailList(org, id, body),
    onSuccess: (list) => {
      client.setQueryData(keys.mailList(org, id), list);
      void client.invalidateQueries({ queryKey: keys.mailLists(org) });
    },
  });
}
