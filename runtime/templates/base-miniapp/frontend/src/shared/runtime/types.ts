import type { AppRole } from '@/shared/roles/role';

export type RuntimeMetric = {
  metric_id: string;
  label: string;
  value: string;
};

export type RuntimeSection =
  | {
      section_id: string;
      type: 'hero';
      title: string;
      body: string;
    }
  | {
      section_id: string;
      type: 'stats';
      items: Array<{ label: string; value: string }>;
    }
  | {
      section_id: string;
      type: 'list';
      items: Array<{ item_id: string; title: string; subtitle: string; status: string; meta?: string }>;
    }
  | {
      section_id: string;
      type: 'detail';
      title: string;
      body: string;
      fields: Array<{ label: string; value: string }>;
    }
  | {
      section_id: string;
      type: 'timeline';
      items: Array<{ label: string; value: string }>;
    }
  | {
      section_id: string;
      type: 'form';
      fields: Array<{
        field_id: string;
        name: string;
        label: string;
        field_type: string;
        required: boolean;
        placeholder?: string;
        value?: string;
      }>;
    }
  | {
      section_id: string;
      type: 'profile';
      fields: Array<{ name: string; label: string; value: string }>;
    };

export type RuntimeAction = {
  action_id: string;
  label: string;
  type: string;
  target_path?: string;
};

export type RuntimeScreen = {
  screen_id: string;
  path: string;
  title: string;
  subtitle?: string;
  kind: string;
  actions: RuntimeAction[];
  sections: RuntimeSection[];
};

export type RuntimeRoute = {
  route_id: string;
  role: AppRole;
  path: string;
  screen_id: string;
  label?: string;
  is_entry: boolean;
};

export type RuntimeRoleManifest = {
  role: AppRole;
  entry_path: string;
  routes: RuntimeRoute[];
  navigation: Array<{ label: string; path: string }>;
  screens: Record<string, RuntimeScreen>;
  metrics: RuntimeMetric[];
  profile: {
    first_name: string;
    last_name?: string;
    email?: string;
    phone?: string;
  };
  alerts: string[];
  activity: Array<{ event_id: string; label: string; role: AppRole }>;
  app: {
    title: string;
    goal: string;
    generation_mode: string;
    route_count: number;
    screen_count: number;
  };
};

export type RuntimeActionResult = {
  status: 'ok' | 'error';
  message?: string | null;
  next_path?: string | null;
  record_id?: string | null;
  refresh_manifest: boolean;
};
