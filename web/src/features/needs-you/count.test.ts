import type { BrandProposal } from '@/api/types';
import type { Approval, CodeRequest, EngageDay, Lane, Question, Run, Workspace } from '@/api/types';
import { countParts, partsWords } from './count';

const NOW = Date.parse('2026-11-07T12:00:00Z');

const yes = (id: string, lane = 'aster/instagram') => ({ id, lane, kind: 'action', state: 'pending' }) as unknown as Approval;
const question = (id: string, lane = 'aster/instagram') => ({ id, lane, asked_by: { kind: 'agent', id: 'claude-code' } }) as unknown as Question;
const failed = (id: string, lane = 'aster/instagram') => ({ id, lane, state: 'approved' }) as unknown as Approval;
const stopped = (id: string, approval: string | null, lane = 'aster/instagram') =>
  ({ id, lane, approval_id: approval, decision_needed: 'Did it go through?' }) as unknown as Run;
const code = (id: string, lane = 'aster/instagram', expires = NOW + 5 * 60_000) =>
  ({ id, lane, state: 'open', expires_at: new Date(expires).toISOString() }) as unknown as CodeRequest;
const plan = (surface: string, approved = false) => ({ surface, program: { id: `pr_${surface}`, version: 1, approved } }) as unknown as EngageDay;
const lane = (id: string, patch: Partial<Lane> = {}) => ({ id, workspace: id.split('/')[0], name: id.split('/')[1], signal: 'ok', state: 'warm', control: { takeback_requested: false }, account: null, login_request: null, ...patch }) as unknown as Lane;
const business = (id: string, lanes: Lane[]) => ({ id, name: id, lanes }) as unknown as Workspace;

describe('the one count of what needs a person', () => {
  it('counts every part once', () => {
    const parts = countParts(
      {
        approvals: [yes('ap_1')],
        questions: [question('q_1')],
        failed: [failed('ap_2')],
        stopped: [stopped('run_1', 'ap_3')],
        plans: [plan('aster/instagram')],
        codes: [code('cr_1')],
        workspaces: [business('aster', [lane('aster/social', { signal: 'captcha' })])],
      },
      null,
      NOW,
    );
    expect(parts).toEqual({ decide: 2, failed: 1, stopped: 1, plans: 1, codes: 1, hands: 1, brand: 0 });
  });

  it('counts a run that stopped halfway once, even when its yes is also a failed outcome', () => {
    const parts = countParts({ failed: [failed('ap_3')], stopped: [stopped('run_1', 'ap_3')] }, null, NOW);
    expect(parts.stopped).toBe(1);
    expect(parts.failed).toBe(0);
  });

  it('leaves out codes that expired and codes that were answered', () => {
    const parts = countParts({ codes: [code('cr_old', 'aster/instagram', NOW - 1000), { ...code('cr_used'), state: 'used' } as unknown as CodeRequest] }, null, NOW);
    expect(parts.codes).toBe(0);
  });

  it('counts an agent\'s login request as an account that needs a hand', () => {
    const asked = lane('aster/social', { login_request: { lane: 'aster/social', why: 'Social signed this lane out.', by: 'claude-code', at: '2026-11-07T11:50:00Z' } });
    expect(countParts({ workspaces: [business('aster', [asked])] }, null, NOW).hands).toBe(1);
  });

  it('narrows every part to one business', () => {
    const parts = countParts(
      {
        approvals: [yes('ap_1', 'aster/instagram'), yes('ap_2', 'harbor/instagram')],
        plans: [plan('harbor/instagram')],
        workspaces: [business('aster', [lane('aster/social', { signal: 'captcha' })]), business('harbor', [lane('harbor/social', { signal: 'captcha' })])],
      },
      'aster',
      NOW,
    );
    expect(parts).toMatchObject({ decide: 1, plans: 0, hands: 1 });
  });

  it('is zero for a list that was never read, so only a read list can say what waits', () => {
    expect(countParts({}, null, NOW)).toEqual({ decide: 0, failed: 0, stopped: 0, plans: 0, codes: 0, hands: 0, brand: 0 });
  });
});

describe('the words for the parts', () => {
  it('says each part that is not zero, and login codes are something to decide', () => {
    expect(partsWords({ decide: 2, failed: 1, stopped: 1, plans: 1, codes: 1, hands: 1, brand: 0 })).toEqual([
      '3 to decide',
      '1 that did not go through',
      '1 run stopped halfway',
      '1 plan to approve',
      '1 account that needs a hand',
    ]);
    expect(partsWords({ decide: 0, failed: 0, stopped: 0, plans: 0, codes: 0, hands: 2, brand: 0 })).toEqual(['2 accounts that need a hand']);
  });

  it('counts a brand change for the owner, in its own workspace, and says it as a change to approve', () => {
    const proposal = (workspace: string): BrandProposal => ({ id: `brand:${workspace}/shop@4`, kind: 'brand', ref: `${workspace}/shop`, workspace, display_name: 'Shop', version: 4, created_at: '', created_by: null, reason: null, diff: [] });
    expect(countParts({ brand: [proposal('harbor'), proposal('aster')] }, null, NOW).brand).toBe(2);
    expect(countParts({ brand: [proposal('harbor'), proposal('aster')] }, 'harbor', NOW).brand).toBe(1);
    expect(partsWords({ decide: 0, failed: 0, stopped: 0, plans: 0, codes: 0, hands: 0, brand: 1 })).toEqual(['1 brand change to approve']);
  });
});
