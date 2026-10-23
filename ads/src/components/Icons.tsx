import React from 'react';

type IconProps = { size?: number; color?: string; stroke?: number; style?: React.CSSProperties };

const Icon: React.FC<IconProps & { children: React.ReactNode }> = ({ size = 24, color = 'currentColor', stroke = 2, style, children }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" style={style}>
    {children}
  </svg>
);

// Paths from Lucide (ISC license), the icon set the VoiceCut editor uses.
export const Sparkles: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
    <path d="M20 3v4" />
    <path d="M22 5h-4" />
    <path d="M4 17v2" />
    <path d="M5 18H3" />
  </Icon>
);

export const Download: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="m7 10 5 5 5-5" />
    <path d="M12 15V3" />
  </Icon>
);

export const Check: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="M20 6 9 17l-5-5" />
  </Icon>
);

export const ChevronDown: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="m6 9 6 6 6-6" />
  </Icon>
);

export const Spinner: React.FC<IconProps & { angle: number }> = ({ angle, ...p }) => (
  <Icon {...p} style={{ ...p.style, transform: `rotate(${angle}deg)` }}>
    <path d="M21 12a9 9 0 1 1-6.219-8.56" />
  </Icon>
);

export const Gear: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
    <circle cx="12" cy="12" r="3" />
  </Icon>
);

export const FileAudio: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
    <path d="M14 2v4a2 2 0 0 0 2 2h4" />
    <path d="M8 12v5" />
    <path d="M12 10v9" />
    <path d="M16 13v3" />
  </Icon>
);

export const Laptop: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="M20 16V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9m16 0H4m16 0 1.28 2.55a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45L4 16" />
  </Icon>
);

export const Key: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4" />
    <path d="m21 2-9.6 9.6" />
    <circle cx="7.5" cy="15.5" r="5.5" />
  </Icon>
);

export const Scissors: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <circle cx="6" cy="6" r="3" />
    <path d="M8.12 8.12 12 12" />
    <path d="M20 4 8.12 15.88" />
    <circle cx="6" cy="18" r="3" />
    <path d="M14.8 14.8 20 20" />
  </Icon>
);

export const Lock: React.FC<IconProps> = (p) => (
  <Icon {...p}>
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
  </Icon>
);

/** The macOS pointer. */
export const Pointer: React.FC<{ size?: number; pressed?: number }> = ({ size = 34, pressed = 0 }) => (
  <svg width={size} height={size * 1.3} viewBox="0 0 20 26" style={{ transform: `scale(${1 - pressed * 0.12})`, transformOrigin: '10% 8%', filter: 'drop-shadow(0 3px 6px rgba(0,0,0,0.5))' }}>
    <path d="M2 1.5 L2 20.5 L6.9 15.8 L10.3 23.6 L13.4 22.3 L10.1 14.6 L16.8 14.6 Z" fill="#fff" stroke="#000" strokeWidth={1.3} strokeLinejoin="round" />
  </svg>
);
