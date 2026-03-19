import type { AppRole } from '@/entities/role/model/role';

export type BootstrapState =
  | {
      status: 'loading';
    }
  | {
      status: 'ready';
      role: AppRole;
    }
  | {
      status: 'error';
      message: string;
    };
