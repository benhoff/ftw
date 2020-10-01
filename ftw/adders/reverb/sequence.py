# python3
# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Sequence adders.

This implements adders which add sequences or partial trajectories.

This module is almost identical to the original acme.adders.reverb.sequence.py, with one bugfix:
In the original module, sequences were never padded because the calculation of how many steps need to be padded
did not work as expected: with an increasing step, the result of this calculation in the original would eventually
become negative, with the result that padding never happens.
This edit of the module fixes this bug and always pads with zeros, if enabled.
The Bugfix can be found in lines 107-115.
"""

from typing import Optional

from acme.adders.reverb import base
from acme.adders.reverb import utils

import reverb
import tree


class SequenceAdder(base.ReverbAdder):
    """An adder which adds sequences of fixed length."""

    def __init__(
            self,
            client: reverb.Client,
            sequence_length: int,
            period: int,
            delta_encoded: bool = False,
            chunk_length: Optional[int] = None,
            priority_fns: Optional[base.PriorityFnMapping] = None,
            pad_end_of_episode: bool = True,
    ):
        """Makes a SequenceAdder instance.

        Args:
          client: See docstring for BaseAdder.
          sequence_length: The fixed length of sequences we wish to add.
          period: The period with which we add sequences. If less than
            sequence_length, overlapping sequences are added. If equal to
            sequence_length, sequences are exactly non-overlapping.
          delta_encoded: If `True` (False by default) enables delta encoding, see
            `Client` for more information.
          chunk_length: Number of timesteps grouped together before delta encoding
            and compression. See `Client` for more information.
          priority_fns: See docstring for BaseAdder.
          pad_end_of_episode: If True (default) then upon end of episode the current
            sequence will be padded (with observations, actions, etc... whose values
            are 0) until its length is `sequence_length`. If False then the last
            sequence in the episode may have length less than `sequence_length`.
        """
        super().__init__(
            client=client,
            buffer_size=sequence_length,
            max_sequence_length=sequence_length,
            delta_encoded=delta_encoded,
            chunk_length=chunk_length,
            priority_fns=priority_fns)

        self._period = period
        self._step = 0
        self._pad_end_of_episode = pad_end_of_episode

    def reset(self):
        self._step = 0
        super().reset()

    def _write(self):
        # Append the previous step and increment number of steps written.
        self._writer.append(self._buffer[-1])
        self._step += 1
        self._maybe_add_priorities()

    def _write_last(self):
        # Create a final step.
        final_step = utils.final_step_like(self._buffer[0], self._next_observation)

        # Append the final step.
        self._buffer.append(final_step)
        self._writer.append(final_step)
        self._step += 1

        # NOTE: this always pads to the fixed length.
        if self._pad_end_of_episode:
            zero_step = tree.map_structure(utils.zeros_like, final_step)
            # Determine how much padding to add. This makes sure that we add (zero)
            # data until the next time we would write a sequence.
            if self._step <= self._max_sequence_length:
                padding = self._max_sequence_length - self._step
            else:
                # The following is the original line from acme.adders.reverb.sequence.py.
                # However, it does not work as expected: as self._step grows, padding will become
                # a negative value at some point and as a result, the for loop below will never be
                # entered because of the negative value passed to range().
                # padding = self._period - (self._step - self._max_sequence_length)
                # The following 3 lines work as expected and always pad to the fixed length (with zeros).
                padding = 0
                if (self._step - self._max_sequence_length) % self._period != 0:
                    padding = self._period - ((self._step - self._max_sequence_length) % self._period)

            # Pad with zeros to get a full sequence.
            for _ in range(padding):
                self._buffer.append(zero_step)
                self._writer.append(zero_step)
                self._step += 1

        # Write priorities for the sequence.
        self._maybe_add_priorities()

    def _maybe_add_priorities(self):
        if not (
                # Write the first time we hit the max sequence length...
                self._step == self._max_sequence_length or
                # ... or every `period`th time after hitting max length.
                (self._step > self._max_sequence_length and
                 (self._step - self._max_sequence_length) % self._period == 0)):
            return

        # Compute priorities for the buffer.
        steps = list(self._buffer)
        num_steps = len(steps)
        table_priorities = utils.calculate_priorities(self._priority_fns, steps)

        # Create a prioritized item for each table.
        for table_name, priority in table_priorities.items():
            self._writer.create_item(table_name, num_steps, priority)
