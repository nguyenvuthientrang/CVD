import sys
import torch
import torch.nn as nn
from utils import *
from arguments import get_args

args = get_args()
if args.drop_type == 'Gauss':
    from dropout.Gauss_dropout import GaussDropoutConv2d as DropoutConv2d
    from dropout.Gauss_dropout import GaussDropout as DropoutLinear
elif args.drop_type == 'AddNoise':
    from dropout.AddNoise import Conv2DAddNoise as DropoutConv2d
    from dropout.AddNoise import LinearAddNoise as DropoutLinear

class Net(nn.Module):
    def __init__(self, inputsize, taskcla):
        super().__init__()
        
        ncha,size,_=inputsize #28
        self.taskcla = taskcla
        
        # if args.conv_Dropout:
        #     self.drop1 = DropoutConv2d(in_channels=ncha, p= args.droprate, size= size)
        self.conv1 = nn.Conv2d(ncha,64,kernel_size=3)
        s = compute_conv_output_size(size,3) #26

        # if args.conv_Dropout:
        #     self.drop2 = DropoutConv2d(in_channels=64, p= args.droprate, size= s)
        self.conv2 = nn.Conv2d(64,64,kernel_size=3)
        s = compute_conv_output_size(s,3) #24
        s = s//2 #12

        if args.conv_Dropout:
            self.drop3 = DropoutConv2d(in_channels=64, p= args.droprate, size= s)
        self.conv3 = nn.Conv2d(64,64,kernel_size=3)
        s = compute_conv_output_size(s,3) #10

        # if args.conv_Dropout:
        #     self.drop4 = DropoutConv2d(in_channels=64, p= args.droprate, size= s)
        self.conv4 = nn.Conv2d(64,64,kernel_size=3)
        s = compute_conv_output_size(s,3) #8
        s = s//2 #4
        if args.conv_Dropout:
            self.drop4 = DropoutConv2d(in_channels=64, p= args.droprate, size= s)
        
        if not args.conv_Dropout:
            self.drop = torch.nn.Dropout(args.droprate_linear)
            self.drop_vd = DropoutLinear(input_size=64*s*s, p= args.droprate)

        self.MaxPool = torch.nn.MaxPool2d(2)
        self.last=torch.nn.ModuleList()
        
        for t,n in self.taskcla:
            self.last.append(torch.nn.Linear(s*s*64,n)) #4*4*64 = 1024
        self.relu = torch.nn.ReLU()

    def forward(self, x, train= False, noise= None):
        kld = []
        if args.conv_Dropout:
            if self.training:
                # if args.conv_Dropout:
                #     h, kl = self.drop1(x)
                #     kld.append(kl)
                h=self.relu(self.conv1(x))

                # if args.conv_Dropout:
                #     h, kl = self.drop2(h)
                #     kld.append(kl)
                h=self.relu(self.conv2(h))
                h=self.MaxPool(h)

                h, kl = self.drop3(h)
                kld.append(kl)
                h=self.relu(self.conv3(h))

                # if args.conv_Dropout:
                #     h, kl = self.drop4(h)
                #     kld.append(kl)
                h=self.relu(self.conv4(h))
                h=self.MaxPool(h)                
                h, kl = self.drop4(h)
                kld.append(kl)

            else:
                # if args.conv_Dropout:
                #     h, kl = self.drop1(x, noise['drop1.log_alpha'])
                #     kld.append(kl)
                h=self.relu(self.conv1(x))

                # if args.conv_Dropout:
                #     h, kl = self.drop2(h, noise['drop2.log_alpha'])
                #     kld.append(kl)
                h=self.relu(self.conv2(h))
                h=self.MaxPool(h)

                h, kl = self.drop3(h, noise['drop3.log_alpha'])
                kld.append(kl)
                h=self.relu(self.conv3(h))

                # if args.conv_Dropout:
                #     h, kl = self.drop4(h, noise['drop4.log_alpha'])
                #     kld.append(kl)
                h=self.relu(self.conv4(h))
                h=self.MaxPool(h)
                h, kl = self.drop4(h, noise['drop4.log_alpha'])
                kld.append(kl)
        else:
            h=self.relu(self.conv1(x))
            h=self.relu(self.conv2(h))
            h=self.drop(self.MaxPool(h))

            h=self.relu(self.conv3(h))
            h=self.relu(self.conv4(h))
            h=self.drop(self.MaxPool(h))               


        h=h.view(x.shape[0],-1)
        if not args.conv_Dropout:
            if self.training:
                h , kl= self.drop_vd(h)
                kld.append(kl)
            else:
                h , kl= self.drop_vd(h, noise['drop_vd.log_alpha'])
                kld.append(kl)

        y = []
        for t,i in self.taskcla:
            y.append(self.last[t](h))

        if self.training:
            return y, sum(kld)
        return y