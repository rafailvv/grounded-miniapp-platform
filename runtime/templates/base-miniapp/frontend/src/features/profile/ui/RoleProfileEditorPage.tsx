import { type ChangeEvent, type ClipboardEvent, type FocusEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react';
import type { AppRole } from '@/entities/role/model/role';
import {
  createEmptyRoleProfileDraft,
  createRoleProfileView,
  getRoleProfileDisplayName,
  loadRoleProfileDraftFromBackend,
  saveRoleProfileDraft,
} from '@/features/profile/model/profileStore';
import { triggerHapticNotification } from '@/shared/telegram/webApp';
import styles from '@/features/profile/ui/RoleProfileEditorPage.module.css';

type RoleProfileEditorPageProps = {
  role: AppRole;
};

type FormErrors = {
  email?: string;
  phone?: string;
};
type SaveState = 'idle' | 'saving' | 'saved';

const EMAIL_REGEXP = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/i;
const REQUIRED_ERROR_PREFIX = 'Enter';
const PHONE_TEMPLATE = '+7 (___) ___-__-__';
const PHONE_EDITABLE_POSITIONS = [4, 5, 6, 9, 10, 11, 13, 14, 16, 17] as const;
const PHONE_LOCAL_DIGITS_COUNT = PHONE_EDITABLE_POSITIONS.length;

function extractPhoneLocalDigits(value: string): string {
  const digits = value.replace(/\D/g, '');
  if (!digits) return '';

  // For values like "+7 (...", ignore the fixed country code.
  if (digits.startsWith('7') || digits.startsWith('8')) {
    if (digits.length === 1) return '';
    return digits.slice(1, 1 + PHONE_LOCAL_DIGITS_COUNT);
  }

  // Pasted local number without country code.
  return digits.slice(0, PHONE_LOCAL_DIGITS_COUNT);
}

function formatPhoneMask(localDigitsRaw: string): string {
  const localDigits = localDigitsRaw.replace(/\D/g, '').slice(0, PHONE_LOCAL_DIGITS_COUNT);
  const chars = PHONE_TEMPLATE.split('');

  PHONE_EDITABLE_POSITIONS.forEach((position, index) => {
    chars[position] = localDigits[index] ?? '_';
  });

  return chars.join('');
}

function getLocalDigitIndexByCaret(caret: number): number {
  let index = 0;
  for (const position of PHONE_EDITABLE_POSITIONS) {
    if (caret > position) {
      index += 1;
    }
  }
  return index;
}

function getCaretByLocalDigitIndex(localDigitIndex: number): number {
  if (localDigitIndex <= 0) return PHONE_EDITABLE_POSITIONS[0];
  if (localDigitIndex >= PHONE_LOCAL_DIGITS_COUNT) {
    return PHONE_EDITABLE_POSITIONS[PHONE_LOCAL_DIGITS_COUNT - 1] + 1;
  }
  return PHONE_EDITABLE_POSITIONS[localDigitIndex];
}

function validateEmail(value: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return 'Enter an email address';
  if (!EMAIL_REGEXP.test(trimmed)) return 'Enter a valid email address';
  return undefined;
}

function validatePhone(value: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return 'Enter a phone number';

  const localDigits = extractPhoneLocalDigits(trimmed);
  if (localDigits.length === 0) {
    return 'Enter a phone number';
  }
  if (localDigits.length !== PHONE_LOCAL_DIGITS_COUNT) {
    return 'Phone number must match the format +7 (999) 123-45-67';
  }

  return undefined;
}

export function RoleProfileEditorPage({ role }: RoleProfileEditorPageProps): JSX.Element {
  const initialDraft = useMemo(() => createEmptyRoleProfileDraft(), []);
  const initialView = useMemo(() => createRoleProfileView(role, initialDraft), [initialDraft, role]);
  const phoneInputRef = useRef<HTMLInputElement | null>(null);

  const [firstName, setFirstName] = useState(initialDraft.firstName);
  const [lastName, setLastName] = useState(initialDraft.lastName);
  const [email, setEmail] = useState(initialDraft.email);
  const [phone, setPhone] = useState(formatPhoneMask(extractPhoneLocalDigits(initialDraft.phone)));
  const [photoUrl, setPhotoUrl] = useState<string | null>(initialDraft.photoUrl);
  const [errors, setErrors] = useState<FormErrors>({});
  const [saveState, setSaveState] = useState<SaveState>('idle');

  useEffect(() => {
    let isMounted = true;

    void loadRoleProfileDraftFromBackend(role).then((storedProfile) => {
      if (!isMounted) return;
      setFirstName(storedProfile.firstName);
      setLastName(storedProfile.lastName);
      setEmail(storedProfile.email);
      setPhone(formatPhoneMask(extractPhoneLocalDigits(storedProfile.phone)));
      setPhotoUrl(storedProfile.photoUrl);
    });

    return () => {
      isMounted = false;
    };
  }, [role]);

  const onPhotoChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = typeof reader.result === 'string' ? reader.result : null;
      setPhotoUrl(dataUrl);
    };
    reader.readAsDataURL(file);
  };

  const onSave = async (): Promise<void> => {
    const nextErrors: FormErrors = {
      email: validateEmail(email),
      phone: validatePhone(phone),
    };

    setErrors(nextErrors);
    if (nextErrors.email || nextErrors.phone) {
      triggerHapticNotification('error');
      setSaveState('idle');
      return;
    }

    setSaveState('saving');

    try {
      await Promise.all([
        saveRoleProfileDraft(role, {
          firstName,
          lastName,
          email,
          phone: extractPhoneLocalDigits(phone) ? phone : '',
          photoUrl,
        }),
        new Promise((resolve) => window.setTimeout(resolve, 450)),
      ]);
      triggerHapticNotification('success');
      setSaveState('saved');
      window.setTimeout(() => setSaveState('idle'), 1500);
    } catch {
      triggerHapticNotification('error');
      setSaveState('idle');
    }
  };

  const onEmailChange = (value: string) => {
    setEmail(value);
    setErrors((prev) => {
      if (!prev.email) return prev;
      if (!prev.email.startsWith(REQUIRED_ERROR_PREFIX)) return prev;
      if (!value.trim()) return prev;
      const next = { ...prev };
      delete next.email;
      return next;
    });
  };

  const clearRequiredPhoneErrorIfFilled = (maskedPhone: string) => {
    setErrors((prev) => {
      if (!prev.phone) return prev;
      if (!prev.phone.startsWith(REQUIRED_ERROR_PREFIX)) return prev;
      if (extractPhoneLocalDigits(maskedPhone).length === 0) return prev;
      const next = { ...prev };
      delete next.phone;
      return next;
    });
  };

  const applyPhoneFromLocalDigits = (localDigits: string, localCaretIndex?: number) => {
    const safeLocalDigits = localDigits.slice(0, PHONE_LOCAL_DIGITS_COUNT);
    const maskedPhone = formatPhoneMask(safeLocalDigits);
    setPhone(maskedPhone);
    clearRequiredPhoneErrorIfFilled(maskedPhone);

    if (typeof localCaretIndex === 'number') {
      window.requestAnimationFrame(() => {
        const input = phoneInputRef.current;
        if (!input) return;
        const nextCaretPosition = getCaretByLocalDigitIndex(localCaretIndex);
        input.setSelectionRange(nextCaretPosition, nextCaretPosition);
      });
    }
  };

  const onPhoneChange = (event: ChangeEvent<HTMLInputElement>) => {
    const rawValue = event.target.value;
    const localDigits = extractPhoneLocalDigits(rawValue);
    const caret = event.target.selectionStart ?? rawValue.length;
    const localCaretIndex = Math.min(getLocalDigitIndexByCaret(caret), localDigits.length);
    applyPhoneFromLocalDigits(localDigits, localCaretIndex);
  };

  const onPhoneKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const selectionStart = input.selectionStart ?? 0;
    const selectionEnd = input.selectionEnd ?? selectionStart;
    const startLocalIndex = getLocalDigitIndexByCaret(selectionStart);
    const endLocalIndex = getLocalDigitIndexByCaret(selectionEnd);
    const currentLocalDigits = extractPhoneLocalDigits(phone);

    if (/^\d$/.test(event.key)) {
      event.preventDefault();
      const nextLocalDigits = `${currentLocalDigits.slice(0, startLocalIndex)}${event.key}${currentLocalDigits.slice(endLocalIndex)}`.slice(
        0,
        PHONE_LOCAL_DIGITS_COUNT,
      );
      const nextLocalCaret = Math.min(startLocalIndex + 1, nextLocalDigits.length);
      applyPhoneFromLocalDigits(nextLocalDigits, nextLocalCaret);
      return;
    }

    if (event.key === 'Backspace') {
      event.preventDefault();
      if (selectionStart !== selectionEnd) {
        const nextLocalDigits = `${currentLocalDigits.slice(0, startLocalIndex)}${currentLocalDigits.slice(endLocalIndex)}`;
        applyPhoneFromLocalDigits(nextLocalDigits, startLocalIndex);
        return;
      }

      if (startLocalIndex === 0) {
        applyPhoneFromLocalDigits(currentLocalDigits, 0);
        return;
      }

      const nextLocalDigits = `${currentLocalDigits.slice(0, startLocalIndex - 1)}${currentLocalDigits.slice(startLocalIndex)}`;
      applyPhoneFromLocalDigits(nextLocalDigits, startLocalIndex - 1);
      return;
    }

    if (event.key === 'Delete') {
      event.preventDefault();
      if (selectionStart !== selectionEnd) {
        const nextLocalDigits = `${currentLocalDigits.slice(0, startLocalIndex)}${currentLocalDigits.slice(endLocalIndex)}`;
        applyPhoneFromLocalDigits(nextLocalDigits, startLocalIndex);
        return;
      }

      const nextLocalDigits = `${currentLocalDigits.slice(0, startLocalIndex)}${currentLocalDigits.slice(startLocalIndex + 1)}`;
      applyPhoneFromLocalDigits(nextLocalDigits, startLocalIndex);
    }
  };

  const onPhonePaste = (event: ClipboardEvent<HTMLInputElement>) => {
    event.preventDefault();

    const pastedText = event.clipboardData.getData('text');
    const pastedDigits = extractPhoneLocalDigits(pastedText);
    const input = event.currentTarget;
    const selectionStart = input.selectionStart ?? 0;
    const selectionEnd = input.selectionEnd ?? selectionStart;
    const startLocalIndex = getLocalDigitIndexByCaret(selectionStart);
    const endLocalIndex = getLocalDigitIndexByCaret(selectionEnd);
    const currentLocalDigits = extractPhoneLocalDigits(phone);
    const nextLocalDigits = `${currentLocalDigits.slice(0, startLocalIndex)}${pastedDigits}${currentLocalDigits.slice(endLocalIndex)}`.slice(
      0,
      PHONE_LOCAL_DIGITS_COUNT,
    );

    applyPhoneFromLocalDigits(nextLocalDigits, Math.min(startLocalIndex + pastedDigits.length, nextLocalDigits.length));
  };

  const onPhoneFocus = (event: FocusEvent<HTMLInputElement>) => {
    const localDigits = extractPhoneLocalDigits(event.currentTarget.value);
    window.requestAnimationFrame(() => {
      const input = phoneInputRef.current;
      if (!input) return;
      const nextCaretPosition = getCaretByLocalDigitIndex(localDigits.length);
      input.setSelectionRange(nextCaretPosition, nextCaretPosition);
    });
  };

  const saveButtonLabel =
    saveState === 'saving' ? 'Saving...' : saveState === 'saved' ? 'Saved' : 'Save';

  return (
    <section className={styles.page}>
      <header className={styles.header}>
        <h2 className={styles.title}>Edit profile</h2>
      </header>

      <div className={styles.previewCard}>
        <div className={styles.avatarSection}>
          <label className={styles.avatarUpload}>
            {photoUrl ? (
              <img
                alt={getRoleProfileDisplayName({ firstName, lastName, roleLabel: initialView.roleLabel })}
                className={styles.avatar}
                src={photoUrl}
              />
            ) : (
              <div className={styles.avatarFallback} aria-hidden="true">
                🙂
              </div>
            )}
            <span className={`material-symbols-outlined ${styles.cameraOverlay}`} aria-hidden="true">
              photo_camera
            </span>
            <input className={styles.hiddenInput} type="file" accept="image/*" onChange={onPhotoChange} />
          </label>
        </div>

        <div className={styles.previewInfo}>
          <strong className={styles.previewName}>{getRoleProfileDisplayName({ firstName, lastName, roleLabel: initialView.roleLabel })}</strong>
        </div>
      </div>

      <div className={styles.formCard}>
        <label className={styles.inputWrapper}>
          <span className={styles.inputLabel}>
            First name<span className={styles.requiredIndicator}> *</span>
          </span>
          <input
            className={styles.textInput}
            value={firstName}
            onChange={(event) => setFirstName(event.target.value)}
            placeholder={firstName ? '' : 'Enter first name'}
          />
        </label>

        <label className={styles.inputWrapper}>
          <span className={styles.inputLabel}>Last name</span>
          <input
            className={styles.textInput}
            value={lastName}
            onChange={(event) => setLastName(event.target.value)}
            placeholder={lastName ? '' : 'Enter last name'}
          />
        </label>

        <label className={styles.inputWrapper}>
          <span className={styles.inputLabel}>Telegram username</span>
          <input className={styles.textInput} value={initialView.username} disabled />
        </label>

        <label className={styles.inputWrapper}>
          <span className={styles.inputLabel}>
            Email<span className={styles.requiredIndicator}> *</span>
          </span>
          <input
            className={styles.textInput}
            value={email}
            onChange={(event) => onEmailChange(event.target.value)}
            type="email"
            placeholder={email ? '' : 'name@example.com'}
          />
          {errors.email ? <span className={styles.error}>{errors.email}</span> : null}
        </label>

        <label className={styles.inputWrapper}>
          <span className={styles.inputLabel}>
            Phone<span className={styles.requiredIndicator}> *</span>
          </span>
          <input
            ref={phoneInputRef}
            className={styles.textInput}
            value={phone}
            onChange={onPhoneChange}
            onKeyDown={onPhoneKeyDown}
            onPaste={onPhonePaste}
            onFocus={onPhoneFocus}
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            placeholder="+7 (___) ___-__-__"
          />
          {errors.phone ? <span className={styles.error}>{errors.phone}</span> : null}
        </label>

        <button
          className={styles.saveButton}
          type="button"
          onClick={() => {
            void onSave();
          }}
          disabled={saveState === 'saving'}
        >
          {saveButtonLabel}
        </button>
      </div>
    </section>
  );
}
