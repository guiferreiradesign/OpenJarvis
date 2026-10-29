import { useState, useCallback, useRef, useEffect } from 'react';
import { transcribeAudio, fetchSpeechHealth } from '../lib/api';
import {
  MAX_RECORDING_MS,
  NO_SPEECH_MESSAGE,
  TRANSCRIPT_ADDED_MESSAGE,
  describeMicError,
  describeTranscriptionError,
  pickRecorderMimeType,
  recordingFilename,
  type SpeechInputState,
} from '../lib/speech-input';

export type SpeechState = SpeechInputState;

interface UseSpeechOptions {
  /** Receives the recognised text. It is only ever placed in the input box. */
  onTranscript?: (text: string) => void;
}

export function useSpeech({ onTranscript }: UseSpeechOptions = {}) {
  const [state, setState] = useState<SpeechState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [available, setAvailable] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const cancelledRef = useRef(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;

  // Check if speech backend is available on mount
  useEffect(() => {
    fetchSpeechHealth()
      .then((health) => setAvailable(health.available))
      .catch(() => setAvailable(false));
  }, []);

  const releaseStream = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  // Stop the microphone and drop any pending upload if the component goes away.
  useEffect(() => {
    return () => {
      cancelledRef.current = true;
      abortRef.current?.abort();
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      releaseStream();
    };
  }, [releaseStream]);

  const finishRecording = useCallback(
    (recorder: MediaRecorder, mimeType: string) => {
      releaseStream();
      mediaRecorderRef.current = null;
      const chunks = chunksRef.current;
      chunksRef.current = [];

      if (cancelledRef.current) {
        setState('idle');
        return;
      }
      const blob = new Blob(chunks, { type: recorder.mimeType || mimeType || 'audio/webm' });
      if (blob.size === 0) {
        setState('idle');
        setError(NO_SPEECH_MESSAGE);
        return;
      }
      setState('transcribing');
      const controller = new AbortController();
      abortRef.current = controller;
      transcribeAudio(blob, recordingFilename(recorder.mimeType || mimeType), controller.signal)
        .then((result) => {
          if (cancelledRef.current) return;
          const text = (result.text || '').trim();
          if (!text) {
            setError(NO_SPEECH_MESSAGE);
          } else {
            onTranscriptRef.current?.(text);
            setNotice(TRANSCRIPT_ADDED_MESSAGE);
          }
        })
        .catch((err: unknown) => {
          if (cancelledRef.current || (err instanceof DOMException && err.name === 'AbortError')) {
            return;
          }
          setError(describeTranscriptionError(err instanceof Error ? err.message : ''));
        })
        .finally(() => {
          abortRef.current = null;
          setState('idle');
        });
    },
    [releaseStream],
  );

  const stopRecording = useCallback((): void => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state === 'recording') recorder.stop();
  }, []);

  /** Discard the recording (or the pending transcription); nothing is uploaded or inserted. */
  const cancelRecording = useCallback((): void => {
    cancelledRef.current = true;
    abortRef.current?.abort();
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state === 'recording') {
      recorder.stop(); // onstop clears everything because of cancelledRef
    } else {
      releaseStream();
      setState('idle');
    }
    setError(null);
    setNotice(null);
  }, [releaseStream]);

  // Recording starts only from an explicit user click on the microphone button.
  const startRecording = useCallback(async (): Promise<void> => {
    setError(null);
    setNotice(null);
    if (mediaRecorderRef.current || state !== 'idle') return;

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('This browser cannot record audio. Use a current Chrome, Edge, Firefox or Safari.');
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      setError(describeMicError(err));
      setState('idle');
      return;
    }

    try {
      const mimeType = pickRecorderMimeType((type) => MediaRecorder.isTypeSupported(type));
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      streamRef.current = stream;
      cancelledRef.current = false;
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => finishRecording(recorder, mimeType);
      recorder.onerror = () => {
        cancelledRef.current = true;
        setError('Recording failed. Try again.');
      };
      recorder.start();
      mediaRecorderRef.current = recorder;
      timeoutRef.current = setTimeout(() => stopRecording(), MAX_RECORDING_MS);
      setState('recording');
    } catch {
      stream.getTracks().forEach((track) => track.stop());
      setError('Could not start recording from the microphone.');
      setState('idle');
    }
  }, [state, finishRecording, stopRecording]);

  return {
    state,
    error,
    notice,
    available,
    startRecording,
    stopRecording,
    cancelRecording,
    isRecording: state === 'recording',
    isTranscribing: state === 'transcribing',
  };
}
