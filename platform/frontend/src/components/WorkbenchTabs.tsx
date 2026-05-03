type WorkbenchTabsProps<T extends string> = {
  tabs: readonly T[];
  activeTab: T;
  onChange: (tab: T) => void;
};

export function WorkbenchTabs<T extends string>({ tabs, activeTab, onChange }: WorkbenchTabsProps<T>) {
  return (
    <div className="tabs">
      {tabs.map((tab) => (
        <button key={tab} type="button" className={tab === activeTab ? "active" : ""} onClick={() => onChange(tab)}>
          {tab}
        </button>
      ))}
    </div>
  );
}
