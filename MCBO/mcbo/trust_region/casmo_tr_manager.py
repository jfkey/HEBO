# Copyright (C) 2020. Huawei Technologies Co., Ltd. All rights reserved.

# This program is free software; you can redistribute it and/or modify it under
# the terms of the MIT license.

# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the MIT License for more details.

from typing import Union, Optional, List, Callable, Dict

import pandas as pd
import torch

from mcbo.acq_funcs import AcqBase
from mcbo.models import ModelBase
from mcbo.models.gp.combo_gp import ComboGPModel, ComboEnsembleGPModel
from mcbo.search_space import SearchSpace
from mcbo.trust_region import TrManagerBase
from mcbo.trust_region.tr_utils import sample_numeric_and_nominal_within_tr
from mcbo.utils.constraints_utils import sample_input_valid_points
from mcbo.utils.data_buffer import DataBuffer
from mcbo.utils.discrete_vars_utils import get_discrete_choices
from mcbo.utils.distance_metrics import hamming_distance
from mcbo.utils.model_utils import move_model_to_device


class CasmopolitanTrManager(TrManagerBase):

    def __init__(self,
                 search_space: SearchSpace,
                 model: ModelBase,
                 acq_func: AcqBase,
                 n_init: int,
                 min_num_radius: Union[int, float],
                 max_num_radius: Union[int, float],
                 init_num_radius: Union[int, float],
                 min_nominal_radius: Union[int, float],
                 max_nominal_radius: Union[int, float],
                 init_nominal_radius: Union[int, float],
                 radius_multiplier: float = 1.5,
                 succ_tol: int = 20,
                 fail_tol: int = 2,
                 restart_n_cand: int = 1000,
                 max_n_perturb_num: int = 20,
                 verbose=False,
                 dtype: torch.dtype = torch.float32,
                 device: torch.device = torch.device('cpu')
                 ):
        super(CasmopolitanTrManager, self).__init__(search_space=search_space, dtype=dtype)

        if not self.search_space.num_cont + self.search_space.num_disc + self.search_space.num_nominal \
               == self.search_space.num_dims:
            raise NotImplementedError(
                'The Casmopolitan Trust region manager only supports continuous, discrete and nominal variables. '
                'If you wish to use the Casmopolitan Trust region manager with ordinal variables,'
                ' model them as nominal variables')

        self.is_numeric = search_space.num_numeric > 0
        self.is_mixed = self.is_numeric and search_space.num_nominal > 0
        self.numeric_dims = self.search_space.cont_dims + self.search_space.disc_dims
        self.discrete_choices = get_discrete_choices(search_space)

        # Register radii for useful variable types
        if search_space.num_numeric > 0:
            self.register_radius('numeric', min_num_radius, max_num_radius, init_num_radius)
        #  if there is only one dim for a variable type: do not use TR for it
        if search_space.num_nominal > 1:
            self.register_radius('nominal', min_nominal_radius, max_nominal_radius, init_nominal_radius)

        self.verbose = verbose
        self.model = model
        self.acq_func = acq_func
        self.n_init = n_init
        self.restart_n_cand = restart_n_cand
        self.max_n_perturb_num = max_n_perturb_num

        self.succ_tol = succ_tol
        self.fail_tol = fail_tol
        self.radius_multiplier = radius_multiplier
        self.device = device

        self.succ_count = 0
        self.fail_count = 0
        self.guided_restart_buffer = DataBuffer(num_dims=self.search_space.num_dims, num_out=1,
                                                dtype=self.data_buffer.dtype)
        assert self.is_numeric or self.search_space.num_nominal > 0

    def adjust_counts(self, y: torch.Tensor):
        if y.min() < self.data_buffer.y.min():  # Originally we had np.min(fX_next) <= tr_min - 1e-3 * abs(tr_min)
            self.succ_count += 1
            self.fail_count = 0
        else:
            self.succ_count = 0
            self.fail_count += 1

    def adjust_tr_radii(self, y: torch.Tensor, **kwargs):
        self.adjust_counts(y=y)

        if self.succ_count == self.succ_tol:  # Expand trust region
            self.succ_count = 0
            if self.is_numeric:
                self.radii['numeric'] = min(self.radii['numeric'] * self.radius_multiplier, self.max_radii['numeric'])
            if self.search_space.num_nominal > 1:
                self.radii['nominal'] = int(
                    min(self.radii['nominal'] * self.radius_multiplier, self.max_radii['nominal']))
            if self.verbose:
                print(f"Expanding trust region...")

        elif self.fail_count == self.fail_tol:  # Shrink trust region
            self.fail_count = 0
            if self.is_numeric:
                self.radii['numeric'] = self.radii['numeric'] / self.radius_multiplier
            if self.search_space.num_nominal > 1:
                self.radii['nominal'] = int(self.radii['nominal'] / self.radius_multiplier)
            if self.verbose:
                print(f"Shrinking trust region...")

    def suggest_new_tr(self, n_init: int, observed_data_buffer: DataBuffer,
                       input_constraints: Optional[List[Callable[[Dict], bool]]],
                       **kwargs) -> pd.DataFrame:
        """
        Function used to suggest a new trust region centre and neighbouring points

        Args:
            n_init:
            input_constraints: list of funcs taking a point as input and outputting whether the point
                                       is valid or not
            observed_data_buffer: Data buffer containing all previously observed points
            kwargs:
        """
        if self.verbose:
            print("Algorithm is stuck in a local optimum. Suggesting new tr....")

        x_init = pd.DataFrame(index=range(n_init), columns=self.search_space.df_col_names, dtype=float)

        # Note, it's not possible to fit the COMBO GP with a single sample
        if not isinstance(self.model, (ComboGPModel, ComboEnsembleGPModel)) or len(self.guided_restart_buffer) >= 1:

            tr_x, tr_y = self.data_buffer.x, self.data_buffer.y

            # store best observed point within current trust region
            best_idx, best_y = self.data_buffer.y_argmin, self.data_buffer.y_min
            self.guided_restart_buffer.append(tr_x[best_idx: best_idx + 1], tr_y[best_idx: best_idx + 1])

            # Determine the device to run on
            move_model_to_device(self.model, self.guided_restart_buffer, self.device)

            # Fit the model
            self.model.fit(self.guided_restart_buffer.x, self.guided_restart_buffer.y)

            # Sample random points and evaluate the acquisition at these points
            x_cand_orig = sample_input_valid_points(n_points=self.restart_n_cand,
                                                            point_sampler=self.search_space.sample,
                                                            input_constraints=input_constraints)
            x_cand = self.search_space.transform(x_cand_orig)
            with torch.no_grad():
                acq = self.acq_func(x_cand, self.model, best_y=best_y)

            # The new trust region centre is the point with the lowest acquisition value
            best_idx = acq.argmin()

            tr_centre = x_cand[best_idx]
            x_init.iloc[0: 1] = self.search_space.inverse_transform(tr_centre.unsqueeze(0))

        else:
            x_init.iloc[0: 1] = sample_input_valid_points(n_points=1, point_sampler=self.search_space.sample,
                                                                  input_constraints=input_constraints)
            tr_centre = self.search_space.transform(x_init.iloc[0:1]).squeeze()

        self.restart_tr()

        # Sample remaining points in the trust region of the new centre
        if self.n_init - 1 > 0:
            # Sample the remaining points
            point_sampler = lambda n_points: self.search_space.inverse_transform(
                sample_numeric_and_nominal_within_tr(
                    x_centre=tr_centre,
                    search_space=self.search_space,
                    tr_manager=self,
                    n_points=n_points,
                    numeric_dims=self.numeric_dims,
                    discrete_choices=self.discrete_choices,
                    max_n_perturb_num=self.max_n_perturb_num,
                    model=self.model,
                    return_numeric_bounds=False
                )
            )
            x_in_tr = sample_input_valid_points(n_points=self.n_init - 1, point_sampler=point_sampler,
                                                        input_constraints=input_constraints)

            # Store them
            x_init.iloc[1: self.n_init] = x_in_tr

        # update data_buffer with previously observed points that are in the same trust region
        x_observed, y_observed = observed_data_buffer.x, observed_data_buffer.y
        for i in range(len(observed_data_buffer)):
            x = x_observed[i:i + 1]

            in_tr = True
            # Check the numeric and hamming distance
            if 'numeric' in self.radii:
                in_tr = ((tr_centre[self.numeric_dims] - x[0, self.numeric_dims]).abs() < self.radii['numeric']).all()
            if 'nominal' in self.radii:
                in_tr = in_tr and (hamming_distance(tr_centre[self.search_space.nominal_dims].unsqueeze(0),
                                                    x[:, self.search_space.nominal_dims],
                                                    False).squeeze() <= self.get_nominal_radius()).item()

            if in_tr:
                self.data_buffer.append(x, y_observed[i:i + 1])

        return x_init

    def restart(self):
        self.restart_tr()
        self.guided_restart_buffer.restart()

    def restart_tr(self):
        super(CasmopolitanTrManager, self).restart_tr()
        self.succ_count = 0
        self.fail_count = 0

    def __getstate__(self):
        d = dict(self.__dict__)
        to_remove = ["model", "search_space"]  # fields to remove when pickling this object
        for attr in to_remove:
            if attr in d:
                del d[attr]
        return d
