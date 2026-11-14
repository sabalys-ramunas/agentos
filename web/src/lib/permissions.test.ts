import { can } from './permissions';

describe('what the memory is', () => {
  it('is the owner\'s to read: no admin, member or viewer reads it', () => {
    expect(can('owner', 'memory.read')).toBe(true);
    for (const role of ['admin', 'member', 'approver', 'viewer'] as const) {
      expect(can(role, 'memory.read')).toBe(false);
    }
  });
});

describe('who may say yes or no to an approval', () => {
  it('is the approver and the admin, never an operator or a viewer', () => {
    expect(can('approver', 'approvals.decide')).toBe(true);
    expect(can('admin', 'approvals.decide')).toBe(true);
    expect(can('owner', 'approvals.decide')).toBe(true);
    expect(can('member', 'approvals.decide')).toBe(false);
    expect(can('viewer', 'approvals.decide')).toBe(false);
  });
});

describe('who may take over an account', () => {
  it('is the owner alone: an operator, an admin and an approver only watch and operate', () => {
    expect(can('owner', 'lanes.control')).toBe(true);
    for (const role of ['admin', 'member', 'approver', 'viewer'] as const) {
      expect(can(role, 'lanes.control')).toBe(false);
    }
    expect(can('member', 'lanes.operate')).toBe(true);
  });
});
