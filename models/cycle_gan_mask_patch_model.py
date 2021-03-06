import torch
import itertools
from util.image_pool import ImagePool
from .base_model import BaseModel
from . import networks
from torch.autograd import Variable
import numpy as np
import torch.nn.functional as F
import kornia.geometry.transform as f

class CycleGANMaskPatchModel(BaseModel):
    #def name(self):
    #    return 'CycleGANModel'

    # new, copied from cyclegansemantic model
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        """Add new dataset-specific options, and rewrite default values for existing options.

        Parameters:
            parser          -- original option parser
            is_train (bool) -- whether training phase or test phase. You can use this flag to add training-specific or test-specific options.

        Returns:
            the modified parser.

        For CycleGAN, in addition to GAN losses, we introduce lambda_A, lambda_B, and lambda_identity for the following losses.
        A (source domain), B (target domain).
        Generators: G_A: A -> B; G_B: B -> A.
        Discriminators: D_A: G_A(A) vs. B; D_B: G_B(B) vs. A.
        Forward cycle loss:  lambda_A * ||G_B(G_A(A)) - A|| (Eqn. (2) in the paper)
        Backward cycle loss: lambda_B * ||G_A(G_B(B)) - B|| (Eqn. (2) in the paper)
        Identity loss (optional): lambda_identity * (||G_A(B) - B|| * lambda_B + ||G_B(A) - A|| * lambda_A) (Sec 5.2 "Photo generation from paintings" in the paper)
        Dropout is not used in the original CycleGAN paper.
        """
        parser.set_defaults(no_dropout=False)  # default CycleGAN did not use dropout, beniz: we do
        if is_train:
            parser.add_argument('--lambda_A', type=float, default=10.0, help='weight for cycle loss (A -> B -> A)')
            parser.add_argument('--lambda_B', type=float, default=10.0, help='weight for cycle loss (B -> A -> B)')
            parser.add_argument('--lambda_identity', type=float, default=0.5, help='use identity mapping. Setting lambda_identity other than 0 has an effect of scaling the weight of the identity mapping loss. For example, if the weight of the identity loss should be 10 times smaller than the weight of the reconstruction loss, please set lambda_identity = 0.1')
            parser.add_argument('--out_mask', action='store_true', help='use loss out mask')
            parser.add_argument('--lambda_out_mask', type=float, default=10.0, help='weight for loss out mask')
            parser.add_argument('--disc_full_im', action='store_true', help='use a discriminator for the full image')
            parser.add_argument('--use_context_G', action='store_true', help='use context for generators')
            parser.add_argument('--train_f_s_B', action='store_true', help='if true f_s will be trained not only on domain A but also on domain B')
        return parser
    
    def __init__(self, opt):
        BaseModel.__init__(self, opt)

        # specify the training losses you want to print out. The program will call base_model.get_current_losses
        losses = ['D_A_full', 'G_A', 'cycle_A', 'idt_A', 
                'D_B_full', 'G_B', 'cycle_B', 'idt_B', 
                ]

        if opt.use_disc_patch:
            losses+=['G_A_2','G_B_2','D_A_patch','D_B_patch']

        self.loss_names = losses
        
        # specify the images you want to save/display. The program will call base_model.get_current_visuals
        visual_names_A = ['real_A', 'fake_B', 'rec_A']

        visual_names_B = ['real_B', 'fake_A', 'rec_B']

        if self.isTrain and self.opt.lambda_identity > 0.0:
           visual_names_A.append('idt_B')
           visual_names_B.append('idt_A') # beniz: inverted for original

        
        
        visual_names_A += ['input_A_label','real_A_out_mask','full_real_A','full_fake_B']
 
        visual_names_B += ['input_B_label','real_B_out_mask','full_real_B','full_fake_A']

       
        self.visual_names = visual_names_A + visual_names_B
            
        # specify the models you want to save to the disk. The program will call base_model.save_networks and base_model.load_networks
        if self.isTrain:
            model_names = ['G_A', 'G_B', 'D_A_full', 'D_B_full']
            if opt.use_disc_patch:
                model_names += ['D_A_patch', 'D_B_patch']
            #self.model_names = ['f_s']
        else:  # during test time, only load Gs
            self.model_names = ['G_A']

        # load/define networks
        # The naming conversion is different from those used in the paper
        # Code (paper): G_A (G), G_B (F), D_A (D_Y), D_B (D_X)

        if opt.use_context_G:
            self.netG_A = networks.define_G(2*opt.input_nc, opt.output_nc,
                                        opt.ngf, opt.netG, opt.norm, 
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
            self.netG_B = networks.define_G(2*opt.output_nc, opt.input_nc,
                                        opt.ngf, opt.netG, opt.norm, 
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
        else:
            self.netG_A = networks.define_G(opt.input_nc, opt.output_nc,
                                        opt.ngf, opt.netG, opt.norm, 
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
            self.netG_B = networks.define_G(opt.output_nc, opt.input_nc,
                                        opt.ngf, opt.netG, opt.norm, 
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)

        if self.isTrain:
            #use_sigmoid = opt.no_lsgan
            self.netD_A_patch = networks.define_D(opt.output_nc, opt.ndf,
                                            opt.netD,
                                            opt.n_layers_D, opt.norm, #use_sigmoid, 
                                            opt.init_type, opt.init_gain, self.gpu_ids)
            self.netD_B_patch = networks.define_D(opt.input_nc, opt.ndf,
                                            opt.netD,
                                            opt.n_layers_D, opt.norm, #use_sigmoid, 
                                            opt.init_type, opt.init_gain, self.gpu_ids)
            
            self.netD_A_full = networks.define_D(opt.output_nc, opt.ndf,
                                            opt.netD,
                                            opt.n_layers_D, opt.norm, #use_sigmoid, 
                                            opt.init_type, opt.init_gain, self.gpu_ids)
            self.netD_B_full = networks.define_D(opt.input_nc, opt.ndf,
                                            opt.netD,
                                            opt.n_layers_D, opt.norm, #use_sigmoid, 
                                            opt.init_type, opt.init_gain, self.gpu_ids)

        if self.isTrain:
            if opt.lambda_identity > 0.0:  # only works when input and output images have the same number of channels
                assert(opt.input_nc == opt.output_nc)
            self.fake_A_pool = ImagePool(opt.pool_size) # create image buffer to store previously generated images
            self.fake_B_pool = ImagePool(opt.pool_size) # create image buffer to store previously generated images
            # define loss functions
            self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)
            self.criterionCycle = torch.nn.L1Loss()
            self.criterionIdt = torch.nn.L1Loss()

            # initialize optimizers
            self.optimizer_G = torch.optim.Adam(itertools.chain(self.netG_A.parameters(), self.netG_B.parameters()),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(itertools.chain(self.netD_A_patch.parameters(), self.netD_B_patch.parameters(),self.netD_A_full.parameters(), self.netD_B_full.parameters()),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizers = []
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)
            #beniz: not adding optimizers f_s (?)

    def set_input(self, input): 
        AtoB = self.opt.direction == 'AtoB'
        self.full_real_A = input['A' if AtoB else 'B'].to(self.device)
        self.full_real_B = input['B' if AtoB else 'A'].to(self.device)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']

        if 'A_label' in input :
            self.input_A_label = input['A_label'].to(self.device).squeeze(1)

            num_batch = self.input_A_label.shape[0]

            self.real_A = torch.tensor(np.zeros(self.full_real_A.size()),dtype=torch.float).to(self.device)
            for k in range(num_batch):

                pos_2 = torch.nonzero(self.input_A_label.squeeze(1)[k])                
                ymin=pos_2[0][0]
                ymax=pos_2[-1][0]
                xmin=pos_2[0][1]
                xmax=pos_2[-1][1]

                size = self.full_real_A.shape[-1]
                temp=f.resize(self.full_real_A[k,:,ymin:ymax+1,xmin:xmax+1].unsqueeze(0),(size,size))
                self.real_A[k] = f.resize(self.full_real_A[k,:,ymin:ymax+1,xmin:xmax+1].unsqueeze(0),(self.full_real_A.shape[-1],self.full_real_A.shape[-1])).squeeze(0)

        if 'B_label' in input:
            self.input_B_label = input['B_label'].to(self.device).squeeze(1)

            num_batch = self.input_B_label.shape[0]


            self.real_B = torch.tensor(np.zeros(self.full_real_B.size()),dtype=torch.float).to(self.device)
            for k in range(num_batch):
                pos_2 = torch.nonzero(self.input_B_label.squeeze(1)[k])
                ymin=pos_2[0][0]
                ymax=pos_2[-1][0]
                xmin=pos_2[0][1]
                xmax=pos_2[-1][1]            
                self.real_B[k] = f.resize(self.full_real_B[k,:,ymin:ymax+1,xmin:xmax+1].unsqueeze(0),(self.full_real_B.shape[-1],self.full_real_B.shape[-1])).squeeze(0)



    def forward(self):
        label_A = self.input_A_label
        label_A_inv = torch.tensor(np.ones(label_A.size()),dtype=torch.float).to(self.device) - label_A
        label_A_inv = label_A_inv.unsqueeze(1)
        label_A_inv = torch.cat ([label_A_inv,label_A_inv,label_A_inv],1)
        
        self.real_A_out_mask = self.full_real_A *label_A_inv

        if self.opt.use_context_G:
            self.fake_B = self.netG_A(torch.cat((self.real_A,self.real_A_out_mask),dim=1))
        else:
            self.fake_B = self.netG_A(self.real_A)
            
        d = 1

        if self.isTrain:

            if self.opt.use_context_G:
                self.rec_A = self.netG_B(torch.cat((self.fake_B,self.real_A_out_mask),dim=1))
            else:     
                self.rec_A = self.netG_B(self.fake_B)
            



            if hasattr(self, 'input_B_label'):
                
                label_B = self.input_B_label
                label_B_inv = torch.tensor(np.ones(label_B.size()),dtype=torch.float).to(self.device) - label_B
                label_B_inv = label_B_inv.unsqueeze(1)
                label_B_inv = torch.cat ([label_B_inv,label_B_inv,label_B_inv],1)
                    
                self.real_B_out_mask = self.full_real_B *label_B_inv
                if self.opt.use_context_G:
                    self.fake_A = self.netG_B(torch.cat((self.real_B,self.real_B_out_mask),dim=1))
                    self.rec_B = self.netG_A(torch.cat((self.fake_A,self.real_B_out_mask),dim=1))
                else:
                    self.fake_A = self.netG_B(self.real_B)
                    self.rec_B = self.netG_A(self.fake_A)
            


        num_batch = self.input_A_label.shape[0]
        self.full_fake_B = torch.tensor(np.zeros(self.full_real_B.size()),dtype=torch.float).to(self.device)
        self.full_fake_A = torch.tensor(np.zeros(self.full_real_A.size()),dtype=torch.float).to(self.device)
        for k in range(num_batch):
            pos = torch.nonzero(self.input_A_label[k])
            ymin=pos[0][0].data
            ymax=pos[-1][0].data
            xmin=pos[0][1].data
            xmax=pos[-1][1].data
            y_c = ymax - ymin + 1
            x_c = xmax - xmin + 1
            fake_B_patch = torch.tensor(np.zeros(self.full_real_B[k].size()),dtype=torch.float).to(self.device)
            fake_B_patch[:,ymin:ymax+1,xmin:xmax+1] = f.resize(self.fake_B[k].unsqueeze(0),(y_c,x_c)).squeeze(0)
            self.full_fake_B[k] = fake_B_patch + self.real_A_out_mask[k]
            
            pos = np.nonzero(self.input_B_label[k])
            ymin=pos[0][0].data
            ymax=pos[-1][0].data
            xmin=pos[0][1].data
            xmax=pos[-1][1].data
            y_c = ymax - ymin + 1
            x_c = xmax - xmin + 1
            fake_A_patch = torch.tensor(np.zeros(self.full_real_A[k].size()),dtype=torch.float).to(self.device)
            fake_A_patch[:,ymin:ymax+1,xmin:xmax+1] = f.resize(self.fake_A[k].unsqueeze(0),(y_c,x_c)).squeeze(0)
            self.full_fake_A[k] = fake_A_patch + self.real_B_out_mask[k]

        


    def backward_D_basic(self, netD, real, fake):
        # Real
        pred_real = netD(real)
        loss_D_real = self.criterionGAN(pred_real, True)
        # Fake
        pred_fake = netD(fake.detach())
        loss_D_fake = self.criterionGAN(pred_fake, False)
        # Combined loss
        loss_D = (loss_D_real + loss_D_fake) * 0.5
        # backward
        loss_D.backward()
        return loss_D
    
    def backward_D_A_patch(self):
        self.loss_D_A_patch = self.backward_D_basic(self.netD_A_patch, self.real_B, self.fake_B)

    def backward_D_B_patch(self):
        self.loss_D_B_patch = self.backward_D_basic(self.netD_B_patch, self.real_A, self.fake_A)

    def backward_D_A_full(self):
        self.loss_D_A_full = self.backward_D_basic(self.netD_A_full, self.full_real_B, self.full_fake_B)

    def backward_D_B_full(self):
        self.loss_D_B_full = self.backward_D_basic(self.netD_B_full, self.full_real_A, self.full_fake_A)


    def backward_G(self):
        #print('BACKWARD G')
        lambda_idt = self.opt.lambda_identity
        lambda_A = self.opt.lambda_A
        lambda_B = self.opt.lambda_B
        # Identity loss
        if lambda_idt > 0:
            # G_A should be identity if real_B is fed.
            if self.opt.use_context_G:
                self.idt_A = self.netG_A(torch.cat((self.real_B,self.real_B_out_mask),dim=1))
                self.idt_B = self.netG_B(torch.cat((self.real_A,self.real_A_out_mask),dim=1))
            else:
                self.idt_A = self.netG_A(self.real_B)
                self.idt_B = self.netG_B(self.real_A)
            self.loss_idt_A = self.criterionIdt(self.idt_A, self.real_B) * lambda_B * lambda_idt
            # G_B should be identity if real_A is fed.
            
            self.loss_idt_B = self.criterionIdt(self.idt_B, self.real_A) * lambda_A * lambda_idt
        else:
            self.loss_idt_A = 0
            self.loss_idt_B = 0

        # GAN loss D_A(G_A(A))
        self.loss_G_A = self.criterionGAN(self.netD_A_full(self.full_fake_B), True)
        if self.opt.use_disc_patch:
            self.loss_G_A_2 = self.criterionGAN(self.netD_A_patch(self.fake_B), True)
        # GAN loss D_B(G_B(B))
        self.loss_G_B = self.criterionGAN(self.netD_B_full(self.full_fake_A), True)
        if self.opt.use_disc_patch:
            self.loss_G_B_2 = self.criterionGAN(self.netD_B_patch(self.fake_A), True)

        # Forward cycle loss
        self.loss_cycle_A = self.criterionCycle(self.rec_A, self.real_A) * lambda_A
        # Backward cycle loss
        self.loss_cycle_B = self.criterionCycle(self.rec_B, self.real_B) * lambda_B
        # combined loss standard cyclegan
        self.loss_G = self.loss_G_A + self.loss_G_B + self.loss_cycle_A + self.loss_cycle_B + self.loss_idt_A + self.loss_idt_B

        if self.opt.use_disc_patch:
            self.loss_G += self.loss_G_A_2 + self.loss_G_B_2
        
        lambda_out_mask = self.opt.lambda_out_mask

        self.loss_G.backward()

    def optimize_parameters(self):
        """Calculate losses, gradients, and update network weights; called in every training iteration"""
        # forward
        self.forward()      # compute fake images and reconostruction images.
        # G_A and G_B
        self.set_requires_grad([self.netD_A_full, self.netD_B_full], False)  # Ds require no gradients when optimizing Gs
        if self.opt.use_disc_patch:
            self.set_requires_grad([self.netD_A_patch, self.netD_B_patch], False)
        self.set_requires_grad([self.netG_A, self.netG_B], True)
        self.optimizer_G.zero_grad()  # set G_A and G_B's gradients to zero
        self.backward_G()             # calculate gradients for G_A and G_B
        self.optimizer_G.step()       # update G_A and G_B's weights
        # D_A and D_B
        self.set_requires_grad([self.netD_A_full, self.netD_B_full], True)
        if self.opt.use_disc_patch:
            self.set_requires_grad([self.netD_A_patch, self.netD_B_patch], True)
        self.optimizer_D.zero_grad()   # set D_A and D_B's gradients to zero
        self.backward_D_A_full()      # calculate gradients for D_A
        self.backward_D_B_full()      # calculate graidents for D_B
        if self.opt.use_disc_patch:
            self.backward_D_A_patch()      # calculate gradients for D_A
            self.backward_D_B_patch()      # calculate graidents for D_B
        self.optimizer_D.step()  # update D_A and D_B's weights
        
